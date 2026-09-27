"""Tests for consent v2 (versioned statements, language, the text shown), the gray-command refusals without a 'yes',
`jevscreen keys set NAME --dialog` (native hidden input box; faked here) and the crawl-descriptions filters.

No network: clients are counted fakes; osascript / zenity never run.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import agent_cli, cli, config, consent, doctor, keys, store  # noqa: E402
from jevscreen.sources import tradingview_profiles  # noqa: E402
from test_screen import seed  # noqa: E402

FAKE_KEY = "sk-or-v1-" + "fake" * 12


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.cfg = config.Config(home=self.home).ensure()
        env = mock.patch.dict(os.environ, {"JEVSCREEN_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        for k in ("OPENROUTER_API_KEY", "JEVSCREEN_OPENROUTER_KEY_FILE"):
            os.environ.pop(k, None)

    def tearDown(self):
        self._tmp.cleanup()

    def main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()


class TestConsentV2(Case):
    def test_record_holds_version_language_and_the_text_shown(self):
        code, out, _ = self.main(["consent", "set", "gray-sources", "yes", "--lang", "zh"])
        self.assertEqual(code, 0)
        rec = json.loads(consent.consent_path(self.cfg).read_text())["topics"]["gray-sources"]
        self.assertEqual((rec["statement_version"], rec["lang"]), (consent.STATEMENT_VERSION, "zh"))
        self.assertEqual(rec["statement"], consent.STATEMENTS["gray-sources"]["zh"])
        self.assertEqual(rec["statement_en"], consent.STATEMENTS["gray-sources"]["en"])
        self.assertIn("OpenRouter", rec["statement_en"])                 # v2 discloses the AI service
        self.assertEqual(consent.get(self.cfg)["statement_version"], consent.STATEMENT_VERSION)
        self.assertEqual(doctor.GRAY_QUESTION, consent.STATEMENTS["gray-sources"]["en"])
        with self.assertRaises(ValueError):
            consent.record(self.cfg, "gray-sources", "yes", lang="fr")

    def test_gray_commands_refuse_without_a_yes_and_send_nothing(self):
        seed(self.cfg, self.home)
        made = []
        with mock.patch.object(cli, "make_client", side_effect=lambda *a, **k: made.append(1)), \
                mock.patch.object(cli, "make_crawl_client", side_effect=lambda *a, **k: made.append(1)):
            for argv in (["refresh-universe"], ["crawl-descriptions", "--limit", "1"], ["import-fd"]):
                code, out, _ = self.main(argv)
                self.assertEqual(code, 1, argv)
                self.assertEqual(json.loads(out)["status"], "consent_required")
        self.assertEqual(made, [])


class TestKeyDialog(Case):
    def runner(self, value="", code=0):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, code, stdout=value + "\n", stderr="")
        return run, calls

    def test_dialog_value_is_stored_and_never_printed(self):
        run, calls = self.runner(FAKE_KEY)
        with mock.patch.object(agent_cli, "read_secret_dialog", lambda name: agent_cli_read(name, run)):
            code, out, err = self.main(["keys", "set", "openrouter", "--dialog"])
        self.assertEqual(code, 0, out + err)
        self.assertNotIn(FAKE_KEY, out + err)
        self.assertEqual(json.loads(out)["via"], "dialog")
        self.assertEqual(self.cfg.openrouter_key(), FAKE_KEY)
        self.assertEqual(oct(keys.key_path(self.cfg, "openrouter").stat().st_mode & 0o777), "0o600")
        self.assertEqual(calls[0][0], "osascript")
        self.assertNotIn(FAKE_KEY, " ".join(calls[0]))

    def test_cancel_or_no_dialog_gives_the_terminal_fallback(self):
        run, _ = self.runner("", code=1)
        with mock.patch.object(agent_cli, "read_secret_dialog", lambda name: agent_cli_read(name, run)):
            code, out, _ = self.main(["keys", "set", "openrouter", "--dialog"])
        data = json.loads(out)
        self.assertEqual((code, data["status"]), (1, "refused"))
        self.assertIn("keys set openrouter", data["human_command"])
        self.assertIn("Never paste the key into the chat", data["text_en"])
        self.assertFalse(keys.key_path(self.cfg, "openrouter").exists())
        self.assertIsNone(agent_cli.dialog_command("openrouter", platform="linux", env={}, which=lambda x: "/b/" + x))
        self.assertEqual(agent_cli.dialog_command("openrouter", platform="linux", env={"DISPLAY": ":0"},
                                                  which=lambda x: "/b/" + x)[:2], ["zenity", "--password"])
        self.assertIsNone(agent_cli.dialog_command("openrouter", platform="win32", env={}, which=lambda x: None))

    def test_the_box_fits_an_agent_timeout_and_a_timeout_says_so(self):
        """Agents kill a command after about 120 s; a new OpenRouter account takes minutes (review R10)."""
        self.assertLessEqual(agent_cli.DIALOG_TIMEOUT_S, 100)
        seen = []

        def run(cmd, **kw):
            seen.append(kw["timeout"])
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        with mock.patch.object(agent_cli, "read_secret_dialog", lambda name: agent_cli_read(name, run)):
            code, out, _ = self.main(["keys", "set", "openrouter", "--dialog"])
        data = json.loads(out)
        self.assertEqual((code, data["status"], data["timed_out"]), (1, "refused", True))
        self.assertLessEqual(seen[0], 100)
        self.assertNotIn("No input box appeared", data["text_en"])
        self.assertEqual(data["retry_command"], "jevscreen keys set openrouter --dialog")
        self.assertIn("keys set openrouter", data["human_command"])

    def test_a_malformed_value_is_refused(self):
        run, _ = self.runner("not a key")
        with mock.patch.object(agent_cli, "read_secret_dialog", lambda name: agent_cli_read(name, run)):
            code, out, _ = self.main(["keys", "set", "openrouter", "--dialog"])
        self.assertEqual(code, 1)
        self.assertNotIn("not a key", out)


ORIG_READ = agent_cli.read_secret_dialog


def agent_cli_read(name, run):
    return ORIG_READ(name, runner=run, platform="darwin", which=lambda x: "/usr/bin/" + x)


class TestCrawlFilters(Case):
    def test_queue_by_country_and_floor(self):
        seed(self.cfg, self.home)
        with store.session(self.cfg) as con:
            con.execute("DELETE FROM descriptions")
            q = tradingview_profiles._queue_rows(con, skip_sec_covered=False)
            jp = tradingview_profiles._queue_rows(con, skip_sec_covered=False, countries=["JP"])
            big = tradingview_profiles._queue_rows(con, skip_sec_covered=False, min_mcap_usd=4e9)
            with self.assertRaises(ValueError):
                tradingview_profiles._queue_rows(con, skip_sec_covered=False, countries=["XX"])
        self.assertEqual(len(q), 7)
        self.assertEqual([r[0] for r in jp], ["TSE:6000"])
        self.assertEqual([r[0] for r in big], ["NASDAQ:BANK", "NYSE:ROBO", "NASDAQ:ROB2"])

    def test_cli_passes_the_filters(self):
        consent.record(self.cfg, "gray-sources", "yes")
        seen = {}

        def fake_crawl(cfg, client, **kw):
            seen.update(kw)
            return {"status": "ok"}
        with mock.patch.object(tradingview_profiles, "crawl", fake_crawl), \
                mock.patch.object(cli, "_cooldown_refusal", lambda *a, **k: None):
            code, out, err = self.main(["crawl-descriptions", "--countries", "CN", "--min-mcap", "1e9",
                                        "--limit", "3"])
        self.assertEqual(code, 0, err)
        self.assertEqual((seen["countries"], seen["min_mcap_usd"]), (["CN"], 1e9))


if __name__ == "__main__":
    unittest.main()
