"""Review fixes on the Jev providers: the human's provider choice is saved (the last Jev key set wins, `keys use`),
a switch says what it re-pays, a rejected key is no dead end, one provider per screen run, Retry-After is capped
(not dropped), doctor's text shows the question and commands its detail points to, clear yes/no words, who receives
the text sent to Jev, and Chinese text without English provider labels. No network and no paid calls: every key is an
invented test value and every transport a local fake."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import agent_cli, consent, doctor, jev, keys, quickstart, screen, store  # noqa: E402
from test_jev_providers import FAKE_OR, FAKE_TS, FAKE_VC, QUESTION, Base, Fake, items  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def run_keys(cfg, *argv) -> tuple[int, dict]:
    import argparse
    p = argparse.ArgumentParser()
    agent_cli.add_parsers(p.add_subparsers(dest="command"))
    args = p.parse_args(list(argv))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = agent_cli.cmd_keys(args, cfg)
    return code, json.loads(buf.getvalue())


class ChoiceTests(Base):
    def test_last_jev_key_set_is_the_one_used(self):
        keys.write_key(self.cfg, "openrouter", FAKE_OR)
        self.assertEqual(jev.resolve_provider(self.cfg)[0].name, "openrouter")
        keys.write_key(self.cfg, "typesafe", FAKE_TS)
        self.assertEqual(jev.resolve_provider(self.cfg), (jev.PROVIDERS["typesafe"], "saved"))
        self.assertEqual(keys.saved_provider(self.cfg), "typesafe")
        keys.write_key(self.cfg, "openrouter", FAKE_OR)
        self.assertEqual(jev.resolve_provider(self.cfg), (jev.PROVIDERS["openrouter"], "saved"))

    def test_from_file_saves_the_choice_too(self):
        keys.write_key(self.cfg, "openrouter", FAKE_OR)
        f = self.home.parent / "vk.txt"
        f.write_text(FAKE_VC + "\n")
        os.chmod(f, 0o600)
        keys.record_key_file(self.cfg, "vercel", f)
        self.assertEqual(jev.resolve_provider(self.cfg), (jev.PROVIDERS["vercel"], "saved"))

    def test_an_exported_key_for_another_tool_does_not_switch_an_existing_openrouter_user(self):
        # an install from before the choice was saved: the key file only, no data/jev_provider
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "openrouter_api_key").write_text(FAKE_OR + "\n")
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": FAKE_TS, "AI_GATEWAY_API_KEY": FAKE_VC}):
            self.assertEqual(jev.resolve_provider(self.cfg), (jev.PROVIDERS["openrouter"], "key"))
            self.assertEqual(quickstart.key_state(self.cfg)["provider"], "openrouter")

    def test_keys_use_switches_without_a_key_and_clear_forgets_the_choice(self):
        keys.write_key(self.cfg, "openrouter", FAKE_OR)
        code, out = run_keys(self.cfg, "keys", "use", "vercel")
        self.assertEqual((code, out["status"], out["active_provider"], out["configured"]), (0, "ok", "vercel", False))
        self.assertIn("keys set vercel", out["warning"])
        self.assertEqual(jev.resolve_provider(self.cfg), (jev.PROVIDERS["vercel"], "saved"))
        c = doctor.jev_provider_check(self.cfg)
        self.assertEqual((c["status"], c["fix_command"]), ("fail", "jevscreen keys set vercel"))
        self.assertIn("keys use openrouter", c["detail"])
        code, out = run_keys(self.cfg, "keys", "use", "openrouter")
        self.assertEqual(out["active_provider"], "openrouter")
        keys.write_key(self.cfg, "typesafe", FAKE_TS)
        self.assertEqual(jev.resolve_provider(self.cfg)[0].name, "typesafe")
        out = keys.clear_key(self.cfg, "typesafe")
        self.assertIsNone(keys.saved_provider(self.cfg))
        self.assertEqual(out["active_provider"], "openrouter")
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                run_keys(self.cfg, "keys", "use", "edinet")

    def test_env_provider_still_wins_and_the_output_says_so(self):
        with mock.patch.dict(os.environ, {"JEVSCREEN_JEV_PROVIDER": "openrouter"}):
            out = keys.write_key(self.cfg, "typesafe", FAKE_TS)
            self.assertEqual(jev.resolve_provider(self.cfg)[1], "explicit")
        self.assertIn("JEVSCREEN_JEV_PROVIDER", out["provider_warning"])

    def test_a_switch_names_what_was_paid_through_the_old_provider(self):
        keys.write_key(self.cfg, "openrouter", FAKE_OR)
        with store.session(self.cfg) as con:
            for i, (prov, cost) in enumerate([(None, 0.25), ("openrouter", 0.05), ("typesafe", 9.0)]):
                con.execute("INSERT INTO jev_requests (request_id, status, cost_usd, provider) VALUES (?, 'ok', ?, ?)",
                            [f"r{i}", cost, prov])
        out = keys.write_key(self.cfg, "typesafe", FAKE_TS)
        sw = out["provider_switch"]
        self.assertEqual((sw["from"], sw["to"], sw["paid_before"]), ("openrouter", "typesafe",
                                                                     {"requests": 2, "usd": 0.3}))
        self.assertEqual(sw["switch_back_command"], "jevscreen keys use openrouter")
        self.assertIn("$0.30", sw["notice_en"])
        self.assertIn("keys use openrouter", sw["notice_zh"])
        self.assertIn("TypeSafe 官方", sw["notice_zh"])
        self.assertNotIn("official API", sw["notice_zh"])
        again = keys.write_key(self.cfg, "typesafe", FAKE_TS)         # same provider: no switch, no notice
        self.assertNotIn("provider_switch", again)

    def test_first_key_is_no_switch(self):
        self.assertNotIn("provider_switch", keys.write_key(self.cfg, "vercel", FAKE_VC))


class RejectedKeyTests(Base):
    def test_rejected_key_then_another_provider_moves_on(self):
        keys.write_key(self.cfg, "typesafe", FAKE_TS)
        k = quickstart.key_state(self.cfg)
        job = {"idea": "纸杯", "canary": {"status": 401, "fingerprint": k["fingerprint"]}}
        item = next(i for i in quickstart.pending(self.cfg, job) if i["id"] == "key_jev")
        self.assertEqual((item["provider"], item["rejected"]), ("typesafe", True))
        self.assertEqual([c["provider"] for c in item["choices"]], ["openrouter", "vercel"])
        self.assertEqual(item["clear_command"], "jevscreen keys clear typesafe")
        self.assertIn("keys set openrouter", item["text_en"])
        self.assertIn("keys set openrouter", item["text_zh"])
        keys.write_key(self.cfg, "openrouter", FAKE_OR)               # the human now uses OpenRouter
        self.assertEqual(quickstart.key_state(self.cfg)["provider"], "openrouter")
        self.assertFalse([i for i in quickstart.pending(self.cfg, job) if i["id"] == "key_jev"])

    def test_env_pinned_provider_offers_no_other_choice(self):
        with mock.patch.dict(os.environ, {"JEVSCREEN_JEV_PROVIDER": "typesafe"}):
            item = quickstart.key_item(self.cfg, quickstart.key_state(self.cfg), rejected=False)
        self.assertNotIn("choices", item)


class OneProviderPerRunTests(Base):
    def test_both_layers_use_the_provider_resolved_at_the_start(self):
        keys.write_key(self.cfg, "openrouter", FAKE_OR)
        factory = screen._run_factory(self.cfg)
        l1 = factory(self.cfg, run_id="r", layer="l1", budget_usd=0.1, dry_run=True)
        keys.write_key(self.cfg, "typesafe", FAKE_TS)                 # set while the run is going
        l2 = factory(self.cfg, run_id="r", layer="l2", budget_usd=0.1, dry_run=True)
        self.assertEqual((l1.provider.name, l2.provider.name), ("openrouter", "openrouter"))

    def test_unknown_env_provider_is_still_reported_by_the_client(self):
        t = Fake(lambda p: {})
        with mock.patch.dict(os.environ, {"JEVSCREEN_JEV_PROVIDER": "anthropic"}):
            factory = screen._run_factory(self.cfg)
            c = factory(self.cfg, run_id="r", layer="l1", budget_usd=0.1, dry_run=False)
            c._transport = t
            with self.assertRaises(jev.JevUnavailable):
                c.classify(items(1), QUESTION)
        self.assertEqual(t.calls, [])


class RetryAfterTests(Base):
    def test_a_long_retry_after_is_capped_not_ignored(self):
        c = self.client(Fake(lambda p: {}), backoff_s=0.0)
        with mock.patch.object(jev, "MAX_RETRY_AFTER_S", 0.2):
            t0 = time.monotonic()
            c._backoff(1, 30.0)
            waited = time.monotonic() - t0
        self.assertGreaterEqual(waited, 0.19)
        self.assertLess(waited, 2.0)
        t0 = time.monotonic()
        c._backoff(1, float("inf"))                                  # not finite: plain backoff (0 here)
        self.assertLess(time.monotonic() - t0, 0.15)


class DoctorTextTests(Base):
    def text(self, *checks) -> str:
        return doctor.format_text({"checks": list(checks), "ok": False, "next_command": None,
                                   "summary": {s: 0 for s in doctor.STATUSES}})

    def test_no_key_text_shows_the_question_and_the_commands(self):
        out = self.text(doctor.jev_provider_check(self.cfg))
        self.assertIn(quickstart.account_question("en"), out)
        for n in jev.PROVIDER_ORDER:
            self.assertIn(f"jevscreen keys set {n} --dialog", out)

    def test_consent_text_shows_the_question_and_both_record_commands(self):
        out = self.text(doctor.consent_check(self.cfg))
        self.assertIn(doctor.GRAY_QUESTION, out)
        self.assertIn(doctor.GRAY_QUESTION_ZH, out)
        self.assertIn("jevscreen consent set gray-sources yes", out)
        self.assertIn("jevscreen consent set gray-sources no", out)


class ConsentAnswerTests(Base):
    def test_clear_yes_and_no_words_are_listed_for_the_agent(self):
        item = next(i for i in quickstart.pending(self.cfg, {"idea": "纸杯"}) if i["id"] == "consent_gray_sources")
        for w in ("可以", "同意", "好", "好的", "行", "yes", "ok"):
            self.assertIn(w, item["answer_words"]["yes"])
        for w in ("不要", "不同意", "不行", "no"):
            self.assertIn(w, item["answer_words"]["no"])
        self.assertFalse(set(consent.ANSWER_WORDS["yes"]) & set(consent.ANSWER_WORDS["no"]))
        self.assertEqual(doctor.consent_check(self.cfg)["answer_words"], consent.ANSWER_WORDS)
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        step5 = agents[agents.index("## Step 5."):agents.index("## Step 6.")]
        for w in ("同意", "好", "行", "OK", "answer_words"):
            self.assertIn(w, step5)

    def test_step_zero_asks_whether_it_is_ok_like_the_statement(self):
        zh = quickstart.STRINGS["zh"]["before"]
        self.assertIn("可不可以", zh)
        self.assertNotIn("要你同意", zh)
        self.assertNotIn("you agree to use", quickstart.STRINGS["en"]["before"])
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn(zh, agents)


class RecipientTests(Base):
    def test_the_consent_question_names_who_receives_the_text(self):
        item = next(i for i in quickstart.pending(self.cfg, {"idea": "纸杯"}) if i["id"] == "consent_gray_sources")
        for w in ("TypeSafe", "OpenRouter", "Vercel"):
            self.assertIn(w, item["note_en"])
        self.assertEqual(item["question_en"], consent.STATEMENTS[consent.GRAY_SOURCES]["en"])   # owner's text as is
        keys.write_key(self.cfg, "openrouter", FAKE_OR)
        items_ = quickstart.pending(self.cfg, {"idea": "纸杯"})
        item = next(i for i in items_ if i["id"] == "consent_gray_sources")
        self.assertIn("OpenRouter", item["note_en"])
        self.assertIn("behind it", item["note_en"])
        self.assertIn("OpenRouter", item["note_zh"])
        text = quickstart.human_text({"idea": "纸杯"}, "needs_human", items_, "zh", None)
        self.assertIn(item["question_zh"] + item["note_zh"], text)
        c = doctor.consent_check(self.cfg)
        self.assertIn("OpenRouter", c["recipient_note"])
        rec = consent.record(self.cfg, consent.GRAY_SOURCES, "yes", lang="zh")
        self.assertIn("OpenRouter", rec["recipient_note"])
        self.assertIn("OpenRouter", rec["recipient_note_en"])

    def test_typesafe_goes_to_typesafe_only(self):
        keys.write_key(self.cfg, "typesafe", FAKE_TS)
        self.assertNotIn("behind", doctor.recipient_note(self.cfg, "en"))
        self.assertIn("TypeSafe", doctor.recipient_note(self.cfg, "zh"))


class ZhLabelTests(Base):
    def test_zh_key_texts_use_chinese_provider_labels(self):
        item = quickstart.key_item(self.cfg, quickstart.key_state(self.cfg), rejected=False)
        self.assertNotIn("official API", item["text_zh"])
        self.assertIn("TypeSafe 官方：`", item["text_zh"])
        self.assertNotIn("`; ", item["text_zh"])
        self.assertIn("TypeSafe (official API): `", item["text_en"])
        keys.write_key(self.cfg, "typesafe", FAKE_TS)
        item = quickstart.key_item(self.cfg, quickstart.key_state(self.cfg), rejected=True, http_status=401)
        self.assertNotIn("official API", item["text_zh"])
        self.assertIn("TypeSafe 官方不接受", item["text_zh"])
        self.assertIn("TypeSafe (official API) rejected", item["text_en"])


if __name__ == "__main__":
    unittest.main()
