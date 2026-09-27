"""The owner's one-language rule on the Jev provider texts: every human-facing text of the provider track (the account
question, the key steps, the provider switch notice, doctor's jev_provider question, the recipient line, consent v4)
exists in Chinese and in English, the English one holds no Chinese, and the Chinese one no English words other than
names, placeholders' values (URLs, commands) and the words the provider sites themselves show (key, credits).

No network, no paid calls; fake keys only.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401

from jevscreen import consent, doctor, jev, keys, quickstart as qs  # noqa: E402
from test_jev_providers import FAKE_OR, FAKE_TS, FAKE_VC, Base  # noqa: E402

HAN = re.compile(r"[一-鿿（），。：；『』「」]")
ZH_OK = {"TypeSafe", "OpenRouter", "Vercel", "AI", "Gateway", "Jev", "key", "HTTP", "API", "Keys", "Create",
         "credits", "TradingView", "Yahoo", "SEC", "BSE", "Mac"}
FAKE = {"openrouter": FAKE_OR, "typesafe": FAKE_TS, "vercel": FAKE_VC}


class ProviderTextsOneLanguage(Base):
    def collect(self, texts: list[tuple[str, str, str | None]]) -> None:
        k = qs.key_state(self.cfg)
        for rej, hs in ((False, None), (True, 401), (True, 403)):
            it = qs.key_item(self.cfg, k, rej, hs)
            for lang in ("zh", "en"):
                texts.append((f"key_item {rej} {hs}", lang, it.get(f"text_{lang}")))
                if it.get("question_en"):
                    texts.append((f"key_item q {rej}", lang, it.get(f"question_{lang}")))
        c = doctor.jev_provider_check(self.cfg)
        if c.get("human_question"):
            texts += [("doctor", "en", c["human_question"]), ("doctor", "zh", c["human_question_zh"])]
        for lang in ("zh", "en"):
            texts.append(("recipient", lang, doctor.recipient_note(self.cfg, lang)))
            for pr in jev.PROVIDER_ORDER:
                for key in ("no_credit", "key_rejected", "key_rejected_vercel"):
                    texts.append((key, lang, qs.STRINGS[lang][key].format(
                        label=qs.provider_label(jev.PROVIDERS[pr], lang), credits_url="https://x")))

    def test_every_provider_text_is_in_one_language(self):
        texts: list[tuple[str, str, str | None]] = []
        for lang in ("zh", "en"):
            texts.append(("consent v4", lang, consent.STATEMENTS[consent.GRAY_SOURCES][lang]))
            texts.append(("account_question", lang, qs.account_question(lang)))
            texts.append(("before", lang, qs.STRINGS[lang]["before"]))
        self.collect(texts)                                    # no Jev key yet: the account question
        for name in ("openrouter", "typesafe", "vercel"):
            out = keys.write_key(self.cfg, name, FAKE[name])
            if out.get("provider_switch"):
                texts += [("switch", lang, out["provider_switch"][f"notice_{lang}"]) for lang in ("zh", "en")]
            self.collect(texts)
        out = keys.use_provider(self.cfg, "openrouter")
        self.assertIn("provider_switch", out)
        texts += [("use", lang, out["provider_switch"][f"notice_{lang}"]) for lang in ("zh", "en")]
        for where, lang, text in texts:
            self.assertTrue(text, f"{where} has no {lang} text")
            if lang == "en":
                self.assertEqual(HAN.findall(text), [], f"{where}: {text}")
            else:
                self.assertTrue(HAN.search(text), f"{where}: {text}")
                plain = re.sub(r"`[^`]*`|https?://\S+", "", text)
                self.assertEqual(set(re.findall(r"[A-Za-z][A-Za-z_\-]*", plain)) - ZH_OK, set(), f"{where}: {text}")


if __name__ == "__main__":
    unittest.main()
