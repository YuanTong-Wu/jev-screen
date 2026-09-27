"""Tests for jevscreen.keywords.

No network and no paid calls: every test injects a fake generator or points the model resolver at a temp directory.
The one test that loads the real local Qwen model is opt-in (JEVSCREEN_TEST_LOCAL_QWEN=1) and runs fully offline.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

try:  # see test_cli_coverage: load numpy before any mock.patch.dict(sys.modules) could drop it
    import numpy  # noqa: F401
except ImportError:
    pass

from jevscreen import keywords  # noqa: E402

IDEA = "企业 AI agent 的身份与权限管控"

GOOD = {
    "idea_en": "Identity and access control for enterprise AI agents.",
    "keywords": {
        "en": ["identity and access management", "privileged access management", "machine identity",
               "zero trust", "access control", "authentication", "authorization", "non-human identity"],
        "zh": ["身份认证", "访问控制", "权限管理", "零信任", "特权账号管理", "身份治理", "统一身份认证", "智能体安全"],
        "ja": ["特権アクセス管理", "認証基盤", "ID管理", "アクセス制御", "ゼロトラスト", "多要素認証", "シングルサインオン",
               "アイデンティティ管理"],
        "ko": ["접근 제어", "통합 인증", "계정 관리", "권한 관리", "제로 트러스트", "다중 인증", "싱글 사인온", "특권 계정 관리"],
    },
}


def fake(outputs):
    """A generator returning the given raw texts in order; records the messages it was called with."""
    outputs = list(outputs)
    calls = []

    def gen(messages, max_new_tokens):
        calls.append({"messages": messages, "max_new_tokens": max_new_tokens})
        return outputs.pop(0)
    gen.calls = calls
    gen.model_name = "fake-qwen"
    return gen


class TempHome(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.cfg = SimpleNamespace(home=self.home)

    def tearDown(self):
        self._tmp.cleanup()


class ParseTests(unittest.TestCase):
    def test_plain_json(self):
        out = keywords.parse_output(json.dumps(GOOD, ensure_ascii=False))
        self.assertEqual(out["idea_en"], GOOD["idea_en"])
        self.assertEqual(out["keywords"]["zh"], GOOD["keywords"]["zh"])
        self.assertEqual(set(out["keywords"]), set(keywords.LANGS))

    def test_fenced_with_prose_and_think_block(self):
        raw = ("<think>{not json}</think>Sure, here it is:\n```json\n" + json.dumps(GOOD, ensure_ascii=False)
               + "\n```\nHope this helps {\"x\": 1}")
        out = keywords.parse_output(raw)
        self.assertEqual(out["keywords"]["ko"][0], "접근 제어")

    def test_skips_broken_leading_brace(self):
        raw = "{oops} then " + json.dumps(GOOD, ensure_ascii=False)
        self.assertEqual(keywords.parse_output(raw)["idea_en"], GOOD["idea_en"])

    def test_clean_dedupe_strip_cap_and_script_filter(self):
        doc = json.loads(json.dumps(GOOD))
        doc["idea_en"] = "  Identity   and access\ncontrol.  "
        doc["keywords"]["en"] = (["  Zero Trust ", "zero trust", "“access control”", "", None, 5, "x" * 200,
                                  "身份认证", "- authentication,"]
                                 + [f"term {i}" for i in range(30)])
        doc["keywords"]["zh"] = GOOD["keywords"]["zh"] + ["特権アクセス管理", "access control", "身份认证"]
        doc["keywords"]["ja"] = GOOD["keywords"]["ja"] + ["접근 제어"]
        doc["keywords"]["ko"] = GOOD["keywords"]["ko"] + ["特権アクセス管理", "AI 에이전트 보안"]
        out = keywords.parse_output(json.dumps(doc, ensure_ascii=False))
        self.assertEqual(out["idea_en"], "Identity and access control.")
        en = out["keywords"]["en"]
        self.assertEqual(en[:3], ["Zero Trust", "access control", "authentication"])
        self.assertEqual(len(en), keywords.MAX_PER_LANG)
        self.assertNotIn("身份认证", en)
        self.assertTrue(all(len(p) <= keywords.MAX_PHRASE_CHARS for p in en))
        self.assertEqual(out["keywords"]["zh"], GOOD["keywords"]["zh"])        # kana, Latin, duplicate dropped
        self.assertNotIn("접근 제어", out["keywords"]["ja"])
        self.assertIn("AI 에이전트 보안", out["keywords"]["ko"])                 # acronym inside Hangul is fine
        self.assertNotIn("特権アクセス管理", out["keywords"]["ko"])

    def test_languages_at_top_level_and_comma_string(self):
        doc = {"idea_en": "x y", **GOOD["keywords"]}
        doc["zh"] = "身份认证，访问控制、权限管理"
        out = keywords.parse_output(json.dumps(doc, ensure_ascii=False))
        self.assertEqual(out["keywords"]["zh"], ["身份认证", "访问控制", "权限管理"])

    def test_prompt_example_echoes_are_dropped_unless_in_idea(self):
        doc = json.loads(json.dumps(GOOD))
        doc["keywords"]["en"] = ["Frozen Food"] + GOOD["keywords"]["en"]
        doc["keywords"]["zh"] = ["冷链物流"] + GOOD["keywords"]["zh"]
        raw = json.dumps(doc, ensure_ascii=False)
        out = keywords.parse_output(raw, IDEA)
        self.assertEqual(out["keywords"]["en"], GOOD["keywords"]["en"])
        self.assertEqual(out["keywords"]["zh"], GOOD["keywords"]["zh"])
        kept = keywords.parse_output(raw, "冷链物流 and frozen food makers")
        self.assertEqual(kept["keywords"]["en"][0], "Frozen Food")
        self.assertEqual(kept["keywords"]["zh"][0], "冷链物流")
        for phrase in ("frozen food", "冷链物流", "냉동식품"):
            self.assertIn(phrase, keywords.build_messages(IDEA)[-1]["content"])

    def test_category_specific_shape_is_merged_categories_first(self):
        doc = {"idea_en": "x", "categories_en": ["identity management", "access control", "身份"],
               "keywords": {lang: {"specific": v[:2], "category": v[2:]} for lang, v in GOOD["keywords"].items()}}
        out = keywords.parse_output(json.dumps(doc, ensure_ascii=False))
        for lang, v in GOOD["keywords"].items():
            self.assertEqual(out["keywords"][lang], v[2:] + v[:2])
        self.assertEqual(out["categories_en"], ["identity management", "access control"])
        self.assertEqual(keywords.parse_output(json.dumps(GOOD, ensure_ascii=False))["categories_en"], [])

    def test_long_idea_en_is_capped(self):
        doc = dict(GOOD, idea_en="word " * 200)
        out = keywords.parse_output(json.dumps(doc, ensure_ascii=False))
        self.assertLessEqual(len(out["idea_en"]), keywords.MAX_IDEA_EN_CHARS + 3)

    def test_invalid_outputs(self):
        cases = {
            "no JSON": "I cannot help with that.",
            "idea_en": json.dumps({"idea_en": 3, "keywords": GOOD["keywords"]}),
            "keywords missing": json.dumps({"idea_en": "x"}),
            "too few": json.dumps({"idea_en": "x", "keywords": dict(GOOD["keywords"], ja=["認証"])},
                                  ensure_ascii=False),
            "wrong script": json.dumps({"idea_en": "x", "keywords": dict(GOOD["keywords"], ko=GOOD["keywords"]["en"])},
                                       ensure_ascii=False),
        }
        for name, raw in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                keywords.parse_output(raw)


class GenerateTests(TempHome):
    def test_generate_writes_cache_and_second_call_hits_it(self):
        gen = fake([json.dumps(GOOD, ensure_ascii=False)])
        r = keywords.generate(self.cfg, IDEA, generator=gen)
        self.assertFalse(r["cached"])
        self.assertEqual(r["model"], "fake-qwen")
        self.assertEqual(r["idea_en"], GOOD["idea_en"])
        self.assertEqual(r["attempts"], 1)
        self.assertIn("load_s", r)
        self.assertIn("gen_s", r)
        path = self.home / "keywords" / f"{keywords.idea_key(IDEA)}.json"
        self.assertTrue(path.is_file())
        self.assertEqual(len(keywords.idea_key(IDEA)), 16)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["keywords"], r["keywords"])
        self.assertIn(IDEA, gen.calls[0]["messages"][-1]["content"])
        self.assertEqual(gen.calls[0]["max_new_tokens"], keywords.MAX_NEW_TOKENS)

        never = fake([])   # would raise IndexError if called
        r2 = keywords.generate(self.cfg, "  " + IDEA + "\n", generator=never)
        self.assertTrue(r2["cached"])
        self.assertEqual(r2["keywords"], r["keywords"])
        self.assertEqual((r2["load_s"], r2["gen_s"]), (0.0, 0.0))
        self.assertEqual(never.calls, [])

    def test_cache_hit_does_not_load_the_model(self):
        keywords.generate(self.cfg, IDEA, generator=fake([json.dumps(GOOD, ensure_ascii=False)]))
        with mock.patch.object(keywords, "_load_model", side_effect=AssertionError("loaded")):
            self.assertTrue(keywords.generate(self.cfg, IDEA)["cached"])

    def test_refresh_bypasses_cache(self):
        keywords.generate(self.cfg, IDEA, generator=fake([json.dumps(GOOD, ensure_ascii=False)]))
        doc = json.loads(json.dumps(GOOD))
        doc["idea_en"] = "Refreshed."
        gen = fake([json.dumps(doc, ensure_ascii=False)])
        r = keywords.generate(self.cfg, IDEA, refresh=True, generator=gen)
        self.assertFalse(r["cached"])
        self.assertEqual(r["idea_en"], "Refreshed.")
        self.assertEqual(len(gen.calls), 1)
        self.assertEqual(keywords.generate(self.cfg, IDEA, generator=fake([]))["idea_en"], "Refreshed.")

    def test_corrupt_or_stale_cache_is_regenerated(self):
        path = keywords.cache_path(self.cfg, IDEA)
        path.parent.mkdir(parents=True)
        for content in ("{not json", json.dumps({"idea": "other idea", "idea_en": "x", "keywords": GOOD["keywords"],
                                                  "prompt_version": keywords.PROMPT_VERSION}),
                        json.dumps({"idea": IDEA, "idea_en": "x", "keywords": GOOD["keywords"],
                                    "prompt_version": keywords.PROMPT_VERSION - 1})):
            with self.subTest(content=content[:30]):
                path.write_text(content, encoding="utf-8")
                gen = fake([json.dumps(GOOD, ensure_ascii=False)])
                self.assertFalse(keywords.generate(self.cfg, IDEA, generator=gen)["cached"])
                self.assertEqual(len(gen.calls), 1)

    def test_retry_once_with_stricter_prompt(self):
        gen = fake(["Here are some keywords: identity, access", json.dumps(GOOD, ensure_ascii=False)])
        r = keywords.generate(self.cfg, IDEA, generator=gen)
        self.assertEqual(r["attempts"], 2)
        self.assertEqual(len(gen.calls), 2)
        second = gen.calls[1]["messages"][-1]["content"]
        self.assertIn("previous answer could not be used", second)
        self.assertIn("no JSON object found", second)
        self.assertNotIn("previous answer", gen.calls[0]["messages"][-1]["content"])

    def test_invalid_twice_raises_and_writes_no_cache(self):
        gen = fake(["nope", json.dumps({"idea_en": "x", "keywords": {"en": ["a"]}})])
        with self.assertRaises(keywords.KeywordsUnavailable) as cm:
            keywords.generate(self.cfg, IDEA, generator=gen)
        self.assertIn("2 attempts", str(cm.exception))
        self.assertEqual(len(gen.calls), 2)
        self.assertFalse(keywords.cache_path(self.cfg, IDEA).exists())

    def test_invalid_output_is_cached_briefly(self):
        bad = fake(["nope", "still nope"])
        with self.assertRaises(keywords.KeywordsUnavailable):
            keywords.generate(self.cfg, IDEA, generator=bad)
        self.assertTrue(keywords.failed_path(self.cfg, IDEA).is_file())
        never = fake([])   # the cached failure answers without generating (or loading the model)
        with mock.patch.object(keywords, "_load_model", side_effect=AssertionError("loaded")), \
                self.assertRaises(keywords.KeywordsUnavailable) as cm:
            keywords.generate(self.cfg, IDEA)
        self.assertIn("--refresh", str(cm.exception))
        with self.assertRaises(keywords.KeywordsUnavailable):
            keywords.generate(self.cfg, IDEA, generator=never)
        self.assertEqual(never.calls, [])
        # expired -> generated again
        with mock.patch.object(keywords.time, "time", return_value=time.time() + keywords.FAILED_TTL_S + 5):
            r = keywords.generate(self.cfg, IDEA, generator=fake([json.dumps(GOOD, ensure_ascii=False)]))
        self.assertFalse(r["cached"])
        self.assertFalse(keywords.failed_path(self.cfg, IDEA).exists())   # cleared by the success

    def test_refresh_ignores_a_cached_failure(self):
        with self.assertRaises(keywords.KeywordsUnavailable):
            keywords.generate(self.cfg, IDEA, generator=fake(["nope", "nope"]))
        r = keywords.generate(self.cfg, IDEA, refresh=True, generator=fake([json.dumps(GOOD, ensure_ascii=False)]))
        self.assertEqual(r["idea_en"], GOOD["idea_en"])

    def test_generator_exception_becomes_unavailable(self):
        def boom(messages, max_new_tokens):
            raise RuntimeError("MPS out of memory")
        with self.assertRaises(keywords.KeywordsUnavailable) as cm:
            keywords.generate(self.cfg, IDEA, generator=boom)
        self.assertIn("MPS out of memory", str(cm.exception))

    def test_empty_idea_is_value_error(self):
        for bad in ("", "   ", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                keywords.generate(self.cfg, bad, generator=fake([]))

    def test_cache_write_failure_still_returns_keywords(self):
        with mock.patch.object(keywords, "_write_cache", side_effect=OSError("read-only")):
            r = keywords.generate(self.cfg, IDEA, generator=fake([json.dumps(GOOD, ensure_ascii=False)]))
        self.assertEqual(r["keywords"]["en"], GOOD["keywords"]["en"])
        self.assertIsNone(r["cache_path"])
        self.assertIn("read-only", r["cache_error"])


class ModelLoadingTests(TempHome):
    def setUp(self):
        super().setUp()
        self._saved_model = keywords._MODEL
        keywords._MODEL = None

    def tearDown(self):
        keywords._MODEL = self._saved_model
        super().tearDown()

    def _snapshot(self, root: Path, name="abc123def4567890") -> Path:
        d = root / "models--Qwen--Qwen3.5-4B" / "snapshots" / name
        d.mkdir(parents=True)
        (d / "config.json").write_text("{}")
        (d / "model.safetensors").write_bytes(b"")
        return d

    def test_force_offline_sets_env(self):
        with mock.patch.dict(os.environ, {"HF_HUB_OFFLINE": "0"}, clear=False):
            os.environ.pop("TRANSFORMERS_OFFLINE", None)
            keywords.force_offline()
            self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")
            self.assertEqual(os.environ["TRANSFORMERS_OFFLINE"], "1")

    def test_load_enforces_offline_even_when_model_missing(self):
        env = {"HF_HUB_CACHE": str(self.home / "empty"), "HF_HOME": str(self.home / "hf")}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(keywords.Path, "home", return_value=self.home):
            os.environ.pop("HF_HUB_OFFLINE", None)
            os.environ.pop("TRANSFORMERS_OFFLINE", None)
            os.environ.pop(keywords.MODEL_ENV, None)
            with self.assertRaises(keywords.KeywordsUnavailable) as cm:
                keywords.generate(self.cfg, IDEA)
            self.assertIn("not found", str(cm.exception))
            self.assertIn("nothing is downloaded", str(cm.exception))
            self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")
            self.assertEqual(os.environ["TRANSFORMERS_OFFLINE"], "1")
        self.assertIsNone(keywords._MODEL)

    def test_resolve_snapshot_from_hub_cache_and_refs(self):
        root = self.home / "hub"
        old = self._snapshot(root, "0000old")
        new = self._snapshot(root, "1111new")
        os.utime(old, (1, 1))
        with mock.patch.dict(os.environ, {"HF_HUB_CACHE": str(root)}, clear=False):
            os.environ.pop(keywords.MODEL_ENV, None)
            self.assertEqual(keywords.resolve_model_dir(), new)
            ref = root / "models--Qwen--Qwen3.5-4B" / "refs" / "main"
            ref.parent.mkdir(parents=True)
            ref.write_text("0000old\n")
            self.assertEqual(keywords.resolve_model_dir(), old)

    def test_model_env_override(self):
        d = self._snapshot(self.home / "custom")
        with mock.patch.dict(os.environ, {keywords.MODEL_ENV: str(d)}):
            self.assertEqual(keywords.resolve_model_dir(), d)
        with mock.patch.dict(os.environ, {keywords.MODEL_ENV: str(self.home / "nope")}):
            with self.assertRaises(keywords.KeywordsUnavailable):
                keywords.resolve_model_dir()

    def test_missing_transformers_is_unavailable(self):
        d = self._snapshot(self.home / "custom")
        with mock.patch.dict(os.environ, {keywords.MODEL_ENV: str(d)}), \
                mock.patch.dict(sys.modules, {"transformers": None}):
            with self.assertRaises(keywords.KeywordsUnavailable) as cm:
                keywords.generate(self.cfg, IDEA)
        self.assertIn("transformers", str(cm.exception))
        self.assertIsNone(keywords._MODEL)

    def test_broken_checkpoint_is_unavailable(self):
        try:
            import torch  # noqa: F401
            import transformers  # noqa: F401
        except ImportError:
            self.skipTest("torch/transformers not installed")
        d = self._snapshot(self.home / "custom")   # empty config/weights: from_pretrained fails locally
        with mock.patch.dict(os.environ, {keywords.MODEL_ENV: str(d)}):
            with self.assertRaises(keywords.KeywordsUnavailable) as cm:
                keywords.generate(self.cfg, IDEA)
        self.assertIn("could not load local model", str(cm.exception))

    def test_model_loaded_once_per_process(self):
        bundle = {"tokenizer": None, "model": None, "device": "cpu", "dir": "x", "name": "Qwen/Qwen3.5-4B@x",
                  "load_s": 1.0}
        calls = []

        def fake_load():
            calls.append(1)
            first = keywords._MODEL is None
            keywords._MODEL = bundle
            return bundle, (1.0 if first else 0.0)

        with mock.patch.object(keywords, "_load_model", side_effect=fake_load), \
                mock.patch.object(keywords, "_local_generator",
                                  return_value=lambda m, n: json.dumps(GOOD, ensure_ascii=False)):
            r1 = keywords.generate(self.cfg, IDEA)
            r2 = keywords.generate(self.cfg, IDEA + " 2")
        self.assertEqual((r1["load_s"], r2["load_s"]), (1.0, 0.0))
        self.assertEqual(r1["model"], "Qwen/Qwen3.5-4B@x")
        # and the real _load_model returns the cached bundle without touching disk
        keywords._MODEL = bundle
        with mock.patch.object(keywords, "resolve_model_dir", side_effect=AssertionError("resolved")):
            self.assertEqual(keywords._load_model(), (bundle, 0.0))


@unittest.skipUnless(os.environ.get("JEVSCREEN_TEST_LOCAL_QWEN") == "1",
                     "set JEVSCREEN_TEST_LOCAL_QWEN=1 to load the local Qwen3.5-4B model (offline)")
class LocalQwenTest(TempHome):
    def test_real_local_model(self):
        with mock.patch.dict(os.environ, {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}):
            r = keywords.generate(self.cfg, IDEA, refresh=True)
        print("\nLOCAL_QWEN_RESULT " + json.dumps(r, ensure_ascii=False, indent=2), file=sys.stderr)
        self.assertFalse(r["cached"])
        self.assertTrue(r["model"].startswith("Qwen/Qwen3.5-4B"))
        for lang in keywords.LANGS:
            self.assertGreaterEqual(len(r["keywords"][lang]), keywords.MIN_PER_LANG, lang)
        self.assertTrue(keywords.generate(self.cfg, IDEA)["cached"])


if __name__ == "__main__":
    unittest.main()
