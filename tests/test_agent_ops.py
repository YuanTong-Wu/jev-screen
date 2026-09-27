"""Tests for agent-first operation: jevscreen doctor / keys / consent. No network: the one OpenRouter check uses a
fake opener. Every test runs in a temporary home with all key variables cleared, so no real key is ever touched."""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import urllib.response
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import agent_cli, cli, consent, doctor, keys, store  # noqa: E402
from jevscreen.config import Config  # noqa: E402

FAKE_OR = "sk-or-v1-" + "ab12" * 16
FAKE_EDINET = "0123456789abcdef0123456789ABCDEF"
FAKE_DART = "f" * 40
FAKE_SEC = "Test Person test.person@example.org"
KEY_ENVS = ("OPENROUTER_API_KEY", "JEVSCREEN_EDINET_API_KEY", "JEVSCREEN_OPENDART_API_KEY", "JEVSCREEN_SEC_USER_AGENT",
            "JEVSCREEN_OPENROUTER_KEY_FILE")
TODAY = dt.date(2026, 9, 27)
SEC_COLS = ("security_id", "exchange", "symbol", "name", "isin", "country", "tv_type", "tv_subtype", "is_primary",
            "company_key", "last_seen_snapshot", "active")


class TempHome(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "data"
        env = {k: v for k, v in os.environ.items() if k not in KEY_ENVS}
        env.update({"JEVSCREEN_HOME": str(self.home)})   # no key variables: only files under this home count
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(home=self.home)

    def run_cli(self, *argv: str, stdin: io.StringIO | None = None, tty: bool = False,
                getpass_value: str | BaseException | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        fake_in = stdin or io.StringIO("")
        fake_in.isatty = lambda: tty  # type: ignore[method-assign]

        def fake_getpass(prompt: str = "") -> str:
            if isinstance(getpass_value, BaseException):
                raise getpass_value
            return getpass_value or ""
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, "stdin", fake_in), mock.patch.object(agent_cli.getpass, "getpass", fake_getpass):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()


def _populate(con, *, as_of: dt.date = TODAY, with_desc: bool = True, with_doc: bool = True) -> None:
    snap = store.record_snapshot(con, source_id="tradingview_scanner", kind="scan", request=None, raw_path=None,
                                 raw_sha256=None, raw_bytes=None, rows=None, duration_s=None)
    rows, market = [], []
    for i, mcap in enumerate((5e9, 2e9, 3e8)):
        sid = f"NYSE:T{i}"
        rows.append((sid, "NYSE", f"T{i}", f"Test {i}", None, "United States", "stock", "common", True,
                     store.company_key(None, sid), snap, True))
        market.append((sid, as_of, mcap, snap))
    store.upsert_many(con, "securities", SEC_COLS, rows)
    store.upsert_many(con, "market_daily", ("security_id", "as_of", "market_cap_usd", "snapshot_id"), market)
    if with_desc:
        store.upsert_many(con, "descriptions", ("security_id", "source_id", "company_key", "text", "snapshot_id"), [
            (f"NYSE:T{i}", "tradingview_profile", store.company_key(None, f"NYSE:T{i}"), "Makes test widgets.", snap)
            for i in range(3)])
    if with_doc:
        store.upsert_many(con, "documents", ("doc_id", "security_id", "company_key", "source_id", "form", "text_path"),
                          [("sec_filing_text:1:a:item1", "NYSE:T0", store.company_key(None, "NYSE:T0"),
                            "sec_filing_text", "10-K", "/nonexistent/item1.txt")])


def _no_key_reads(test: unittest.TestCase):
    """Fail the test if any key file's content is read."""
    names = {s.filename for s in keys.KEYS.values()}
    original = Path.read_text

    def guarded(self, *a, **k):
        if self.name in names:
            raise AssertionError(f"key file read: {self.name}")
        return original(self, *a, **k)
    return mock.patch.object(Path, "read_text", guarded)


# ------------------------------------------------------------------------------------------------------ keys


class KeysLibraryTest(TempHome):
    def test_write_key_mode_0600_and_no_value_in_summary(self) -> None:
        out = keys.write_key(self.cfg, "openrouter", FAKE_OR + "\n")
        path = self.home / "openrouter_api_key"
        self.assertEqual(path.read_text().strip(), FAKE_OR)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertTrue(out["shape_ok"])
        self.assertNotIn(FAKE_OR, json.dumps(out))
        self.assertEqual(self.cfg.openrouter_key(), FAKE_OR)   # the adapter reads what keys set wrote

    def test_every_name_uses_the_file_the_adapters_read(self) -> None:
        for name, value, getter in (("edinet", FAKE_EDINET, self.cfg.edinet_api_key),
                                    ("opendart", FAKE_DART, self.cfg.opendart_api_key),
                                    ("sec-email", FAKE_SEC, self.cfg.sec_user_agent)):
            keys.write_key(self.cfg, name, value)
            self.assertEqual(getter(), value, name)

    def test_normalize_strips_quotes_and_newlines(self) -> None:
        self.assertEqual(keys.normalize(f'  "{FAKE_OR}"\n'), FAKE_OR)
        self.assertEqual(keys.normalize(f"'{FAKE_DART}'"), FAKE_DART)

    def test_bad_shapes_are_refused_without_quoting_the_value(self) -> None:
        bad = {"openrouter": "pk-live-" + "x" * 30, "edinet": "short", "opendart": "g" * 39 + "!",
               "sec-email": "no-email-here-at-all"}
        for name, value in bad.items():
            with self.assertRaises(keys.KeyProblem) as cm:
                keys.write_key(self.cfg, name, value)
            self.assertNotIn(value, str(cm.exception), name)
            self.assertFalse((self.home / keys.KEYS[name].filename).exists(), name)
        with self.assertRaises(keys.KeyProblem):
            keys.write_key(self.cfg, "sec-email", "only@example.org")   # a name is required too
        with self.assertRaises(keys.KeyProblem):
            keys.write_key(self.cfg, "openrouter", "sk-or-v1-abc def ghi jkl mno")

    def test_skip_shape_check_still_refuses_empty_and_control_characters(self) -> None:
        keys.write_key(self.cfg, "edinet", "odd-but-real-key", check_shape=False)
        self.assertEqual(self.cfg.edinet_api_key(), "odd-but-real-key")
        for value in ("", "   ", "abc\tdef"):
            with self.assertRaises(keys.KeyProblem):
                keys.write_key(self.cfg, "edinet", value, check_shape=False)

    def test_env_variable_wins_and_is_reported(self) -> None:
        with mock.patch.dict(os.environ, {"JEVSCREEN_OPENDART_API_KEY": FAKE_DART}):
            out = keys.write_key(self.cfg, "opendart", "e" * 40)
            self.assertIn("JEVSCREEN_OPENDART_API_KEY", out["warning"])
            self.assertEqual(keys.presence(self.cfg, "opendart")["source"], "env")

    def test_presence_is_stat_only_and_check_reports_shape(self) -> None:
        keys.write_key(self.cfg, "edinet", FAKE_EDINET)
        with _no_key_reads(self):
            rows = [keys.presence(self.cfg, n) for n in keys.KEYS]
        by = {r["name"]: r for r in rows}
        self.assertTrue(by["edinet"]["configured"])
        self.assertTrue(by["edinet"]["mode_ok"])
        self.assertFalse(by["openrouter"]["configured"])
        (self.home / "opendart_api_key").write_text("not a dart key\n")
        chk = keys.check(self.cfg, "opendart")
        self.assertTrue(chk["configured"])
        self.assertFalse(chk["shape_ok"])
        self.assertNotIn("not a dart key", json.dumps(chk))
        self.assertTrue(keys.check(self.cfg, "edinet")["shape_ok"])

    def test_empty_file_counts_as_absent_and_loose_mode_is_flagged(self) -> None:
        self.home.mkdir(parents=True)
        (self.home / "edinet_api_key").write_text("")
        self.assertFalse(keys.presence(self.cfg, "edinet")["configured"])
        keys.write_key(self.cfg, "edinet", FAKE_EDINET)
        os.chmod(self.home / "edinet_api_key", 0o644)
        self.assertFalse(keys.presence(self.cfg, "edinet")["mode_ok"])

    def test_openrouter_candidates_are_the_named_file_or_the_home_file(self) -> None:
        """Mirrors Config.openrouter_key_source(): a named file is the only candidate; there is no legacy path."""
        self.assertEqual(self.cfg.openrouter_key_files(), [self.home / "openrouter_api_key"])
        named = Path(self.tmp.name) / "named_key_file"
        with mock.patch.dict(os.environ, {"JEVSCREEN_OPENROUTER_KEY_FILE": str(named)}):
            self.assertEqual(self.cfg.openrouter_key_files(), [named])
            keys.write_key(self.cfg, "openrouter", FAKE_OR)       # home file exists, but the named one is missing
            p = keys.presence(self.cfg, "openrouter")
            self.assertEqual((p["configured"], p["source"]), (False, "none"))
            self.assertIsNone(self.cfg.openrouter_key())

    def test_clear_key(self) -> None:
        keys.write_key(self.cfg, "opendart", FAKE_DART)
        self.assertTrue(keys.clear_key(self.cfg, "opendart")["removed"])
        self.assertFalse(keys.clear_key(self.cfg, "opendart")["removed"])
        self.assertIsNone(self.cfg.opendart_api_key())

    def test_clear_warns_when_another_source_still_provides_the_key(self) -> None:
        keys.write_key(self.cfg, "edinet", FAKE_EDINET)
        with mock.patch.dict(os.environ, {"JEVSCREEN_EDINET_API_KEY": FAKE_EDINET}):
            out = keys.clear_key(self.cfg, "edinet")
        self.assertTrue(out["removed"])
        self.assertIn("JEVSCREEN_EDINET_API_KEY", out["warning"])
        self.assertNotIn(FAKE_EDINET, json.dumps(out))
        self.assertNotIn("warning", keys.clear_key(self.cfg, "edinet"))

    def test_stale_temp_file_is_replaced(self) -> None:
        self.home.mkdir(parents=True)
        (self.home / f".edinet_api_key.{os.getpid()}.tmp").write_text("leftover")
        keys.write_key(self.cfg, "edinet", FAKE_EDINET)
        self.assertEqual(self.cfg.edinet_api_key(), FAKE_EDINET)
        self.assertEqual([p.name for p in self.home.iterdir()], ["edinet_api_key"])

    def test_presence_mirrors_config_resolution(self) -> None:
        """An empty first file or a blank variable is what Config returns (empty key): never report 'configured'."""
        legacy = Path(self.tmp.name) / "named_key_file"
        legacy.write_text(FAKE_OR)
        self.home.mkdir(parents=True)
        (self.home / "openrouter_api_key").write_text("")
        with mock.patch.dict(os.environ, {"JEVSCREEN_OPENROUTER_KEY_FILE": str(self.home / "openrouter_api_key")}):
            p = keys.presence(self.cfg, "openrouter")
            self.assertEqual((p["configured"], p["source"]), (False, "file"))
            self.assertEqual(self.cfg.openrouter_key(), "")
        with mock.patch.dict(os.environ, {"JEVSCREEN_EDINET_API_KEY": "   "}):
            keys.write_key(self.cfg, "edinet", FAKE_EDINET)
            p = keys.presence(self.cfg, "edinet")
            self.assertEqual((p["configured"], p["source"]), (False, "env"))
            self.assertEqual(self.cfg.edinet_api_key(), "")

    def test_set_openrouter_warns_when_a_named_file_shadows_it(self) -> None:
        named = Path(self.tmp.name) / "old_key"
        named.write_text("sk-or-v1-old-old-old-old")
        with mock.patch.dict(os.environ, {"JEVSCREEN_OPENROUTER_KEY_FILE": str(named)}):
            out = keys.write_key(self.cfg, "openrouter", FAKE_OR)
        self.assertIn(str(named), out["warning"])
        self.assertNotIn("warning", keys.write_key(self.cfg, "openrouter", FAKE_OR))   # named file absent

    def test_unknown_name(self) -> None:
        with self.assertRaises(keys.KeyProblem):
            keys.spec("github")


class KeysCliTest(TempHome):
    def test_set_on_a_terminal_uses_the_hidden_prompt(self) -> None:
        code, out, err = self.run_cli("keys", "set", "openrouter", tty=True, getpass_value=FAKE_OR)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], "ok")
        self.assertNotIn(FAKE_OR, out + err)
        self.assertEqual(self.cfg.openrouter_key(), FAKE_OR)

    def test_set_without_terminal_needs_stdin_flag(self) -> None:
        code, out, err = self.run_cli("keys", "set", "openrouter", stdin=io.StringIO(FAKE_OR + "\n"))
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["status"], "refused")
        self.assertFalse((self.home / "openrouter_api_key").exists())
        self.assertNotIn(FAKE_OR, out + err)

    def test_set_with_stdin_flag(self) -> None:
        code, out, err = self.run_cli("keys", "set", "sec-email", "--stdin", stdin=io.StringIO(FAKE_SEC + "\n"))
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("test.person", out + err)
        self.assertEqual(self.cfg.sec_user_agent(), FAKE_SEC)

    def test_bad_value_refused_and_not_echoed(self) -> None:
        secret = "sk-live-" + "Z9" * 20
        code, out, err = self.run_cli("keys", "set", "openrouter", "--stdin", stdin=io.StringIO(secret))
        self.assertEqual(code, 1)
        self.assertNotIn(secret, out + err)
        self.assertIn("sk-or-", json.loads(out)["error"])

    def test_cancel_at_the_prompt(self) -> None:
        code, out, err = self.run_cli("keys", "set", "edinet", tty=True, getpass_value=KeyboardInterrupt())
        self.assertEqual(code, 130)
        self.assertFalse((self.home / "edinet_api_key").exists())

    def test_check_named_and_all(self) -> None:
        code, out, _ = self.run_cli("keys", "check", "openrouter")
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["keys"][0]["configured"])
        keys.write_key(self.cfg, "openrouter", FAKE_OR)
        code, out, _ = self.run_cli("keys", "check", "openrouter")
        self.assertEqual(code, 0)
        code, out, _ = self.run_cli("keys", "check")
        self.assertEqual(code, 0)
        rows = json.loads(out)["keys"]
        self.assertEqual([r["name"] for r in rows], list(keys.KEYS))
        self.assertNotIn(FAKE_OR, out)

    def test_clear(self) -> None:
        keys.write_key(self.cfg, "edinet", FAKE_EDINET)
        code, out, _ = self.run_cli("keys", "clear", "edinet")
        self.assertEqual((code, json.loads(out)["removed"]), (0, True))

    def test_real_process_pipe(self) -> None:
        """The non-terminal path in a real subprocess: value piped with --stdin, never printed."""
        env = dict(os.environ, PYTHONPATH=str(SRC))
        p = subprocess.run([sys.executable, "-m", "jevscreen.cli", "keys", "set", "opendart", "--stdin"],
                           input=FAKE_DART + "\n", capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn(FAKE_DART, p.stdout + p.stderr)
        self.assertEqual(stat.S_IMODE((self.home / "opendart_api_key").stat().st_mode), 0o600)
        p = subprocess.run([sys.executable, "-m", "jevscreen.cli", "keys", "set", "opendart"],
                           input=FAKE_DART + "\n", capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(p.returncode, 1)
        self.assertNotIn(FAKE_DART, p.stdout + p.stderr)


# ------------------------------------------------------------------------------------------------------ consent


class ConsentTest(TempHome):
    def test_unset_means_no(self) -> None:
        self.assertEqual(consent.get(self.cfg)["state"], "unset")
        self.assertFalse(consent.gray_sources_allowed(self.cfg))
        with self.assertRaises(consent.ConsentRequired) as cm:
            consent.require_gray_sources(self.cfg, "tradingview_profile")
        self.assertEqual(cm.exception.state, "unset")

    def test_official_sources_never_need_consent_unknown_ones_do(self) -> None:
        for sid in ("sec_filing_text", "sec_tickers", "cninfo_annual_report", "edinet_yuho", "dart_business_report"):
            consent.require_gray_sources(self.cfg, sid)
        with self.assertRaises(consent.ConsentRequired):
            consent.require_gray_sources(self.cfg, "some_new_scraper")

    def test_record_yes_then_no_keeps_history(self) -> None:
        out = consent.record(self.cfg, "gray-sources", "YES")
        self.assertEqual(out["value"], "yes")
        self.assertTrue(out["recorded_at"].endswith("Z"))
        consent.require_gray_sources(self.cfg, "tradingview_scanner")
        self.assertTrue(consent.gray_sources_allowed(self.cfg))
        consent.record(self.cfg, "gray-sources", "no")
        self.assertFalse(consent.gray_sources_allowed(self.cfg))
        data = json.loads((self.home / "consent.json").read_text())
        self.assertEqual([h["value"] for h in data["history"]], ["yes", "no"])
        self.assertIn("personal research", data["topics"]["gray-sources"]["statement"])

    def test_malformed_file_fails_closed_and_is_kept_aside(self) -> None:
        self.home.mkdir(parents=True)
        (self.home / "consent.json").write_text("{not json")
        self.assertEqual(consent.get(self.cfg)["state"], "unreadable")
        with self.assertRaises(consent.ConsentRequired):
            consent.require_gray_sources(self.cfg, "tradingview_scanner")
        consent.record(self.cfg, "gray-sources", "yes")
        self.assertEqual(len(list(self.home.glob("consent.json.bad-*"))), 1)
        (self.home / "consent.json").write_text("[]")
        consent.record(self.cfg, "gray-sources", "no")
        self.assertEqual(len(list(self.home.glob("consent.json.bad-*"))), 2)   # earlier copy kept
        (self.home / "consent.json").write_text(json.dumps({"topics": {"gray-sources": {"value": "maybe"}}}))
        self.assertEqual(consent.get(self.cfg)["state"], "unset")

    def test_invalid_topic_or_value(self) -> None:
        with self.assertRaises(ValueError):
            consent.record(self.cfg, "everything", "yes")
        with self.assertRaises(ValueError):
            consent.record(self.cfg, "gray-sources", "sure")

    def test_cli_set_and_show(self) -> None:
        code, out, _ = self.run_cli("consent", "set", "gray-sources", "yes")
        self.assertEqual((code, json.loads(out)["value"]), (0, "yes"))
        code, out, _ = self.run_cli("consent", "show")
        shown = json.loads(out)
        self.assertEqual(shown["topics"]["gray-sources"]["state"], "yes")
        self.assertEqual(len(shown["history"]), 1)
        with self.assertRaises(SystemExit):
            self.run_cli("consent", "set", "gray-sources", "maybe")


# ------------------------------------------------------------------------------------------------------ doctor


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class DoctorTest(TempHome):
    def make_store(self, **kw) -> None:
        with store.session(self.cfg) as con:
            _populate(con, **kw)

    def make_ready(self) -> None:
        self.make_store()
        keys.write_key(self.cfg, "openrouter", FAKE_OR)
        consent.record(self.cfg, "gray-sources", "yes")

    @staticmethod
    def by_id(result) -> dict:
        return {c["id"]: c for c in result["checks"]}

    def test_empty_home_points_to_init(self) -> None:
        r = doctor.run(self.cfg, today=TODAY)
        self.assertFalse(r["ok"])
        self.assertEqual(r["next_command"], "jevscreen init")
        c = self.by_id(r)
        self.assertEqual(c["store"]["status"], "fail")
        self.assertEqual(c["key_openrouter"]["status"], "fail")
        self.assertTrue(c["key_openrouter"]["ask_human"])
        self.assertEqual(c["key_edinet"]["status"], "skip")   # off by default
        self.assertFalse(self.cfg.db_path.exists())           # doctor never creates the store

    def test_schema(self) -> None:
        r = doctor.run(self.cfg, today=TODAY)
        for k in ("command", "ok", "ready_for", "checks", "next_command", "ask_human", "summary",
                  "first_run_defaults"):
            self.assertIn(k, r)
        ids = [c["id"] for c in r["checks"]]
        self.assertEqual(len(ids), len(set(ids)))
        for c in r["checks"]:
            self.assertTrue({"id", "status", "detail", "fix_command", "ask_human"} <= set(c))
            self.assertIn(c["status"], doctor.STATUSES)
        self.assertEqual(sum(r["summary"].values()), len(r["checks"]))
        json.dumps(r)

    def test_ready_install(self) -> None:
        self.make_ready()
        with _no_key_reads(self):
            r = doctor.run(self.cfg, today=TODAY)
        self.assertTrue(r["ok"], [c for c in r["checks"] if c["status"] == "fail"])
        self.assertEqual(r["ready_for"], "screen")
        self.assertEqual(r["next_command"], doctor.FIRST_SCREEN)
        self.assertIn("--min-mcap 1e9 --budget 1 --dry-run", r["next_command"])
        c = self.by_id(r)
        self.assertEqual(c["universe"]["companies"], 3)
        self.assertEqual(c["universe"]["age_days"], 0)
        self.assertEqual((c["descriptions"]["companies"], c["descriptions"]["with_description"]), (2, 2))
        self.assertEqual(c["official_text"]["with_official_text"], 1)
        self.assertEqual(c["consent_gray_sources"]["state"], "yes")
        self.assertEqual(c["jev"]["status"], "skip")
        self.assertNotIn(FAKE_OR, json.dumps(r))

    def test_cli_exit_codes_and_json(self) -> None:
        code, out, _ = self.run_cli("doctor", "--json")
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["ok"])
        self.make_ready()
        code, out, _ = self.run_cli("doctor", "--json")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["ok"])
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 0)
        self.assertIn("READY", out)
        self.assertIn("next: jevscreen screen", out)

    def test_stale_universe_and_low_coverage_warn(self) -> None:
        self.make_store(as_of=TODAY - dt.timedelta(days=40), with_desc=False, with_doc=False)
        consent.record(self.cfg, "gray-sources", "yes")
        c = self.by_id(doctor.run(self.cfg, today=TODAY))
        self.assertEqual(c["universe"]["status"], "warn")
        self.assertEqual(c["universe"]["fix_command"], "jevscreen refresh-universe")
        self.assertEqual(c["descriptions"]["status"], "warn")
        self.assertEqual(c["official_text"]["status"], "warn")

    def test_empty_universe_asks_for_consent_first(self) -> None:
        with store.session(self.cfg):
            pass
        r = doctor.run(self.cfg, today=TODAY)
        c = self.by_id(r)
        self.assertEqual(c["universe"]["status"], "fail")
        self.assertIsNone(c["universe"]["fix_command"])              # a decision, not a command to run
        self.assertTrue(c["universe"]["ask_human"])
        self.assertEqual(c["universe"]["human_question"], doctor.GRAY_QUESTION)
        self.assertEqual(c["universe"]["record_answer_commands"], doctor.GRAY_RECORD_COMMANDS)
        self.assertEqual(doctor.GRAY_RECORD_COMMANDS, ["jevscreen consent set gray-sources yes",
                                                       "jevscreen consent set gray-sources no"])
        self.assertIn("universe", r["ask_human"])
        self.assertNotIn("consent", r["next_command"] or "")
        g = c["consent_gray_sources"]
        self.assertIsNone(g["fix_command"])
        self.assertEqual((g["human_question"], g["record_answer_commands"]),
                         (doctor.GRAY_QUESTION, doctor.GRAY_RECORD_COMMANDS))
        consent.record(self.cfg, "gray-sources", "yes")
        c = self.by_id(doctor.run(self.cfg, today=TODAY))
        self.assertEqual(c["universe"]["fix_command"], "jevscreen refresh-universe")
        self.assertFalse(c["universe"]["ask_human"])
        self.assertNotIn("human_question", c["universe"])

    def test_gray_question_is_the_one_in_agents_md(self) -> None:
        text = (SRC.parent / "AGENTS.md").read_text()
        quoted = " ".join(line[2:].strip() for line in text.splitlines() if line.startswith("> "))
        self.assertIn(doctor.GRAY_QUESTION, quoted)

    def test_declined_consent_with_empty_universe_never_points_at_consent(self) -> None:
        with store.session(self.cfg):
            pass
        consent.record(self.cfg, "gray-sources", "no")
        r = doctor.run(self.cfg, today=TODAY)
        c = self.by_id(r)
        self.assertEqual(c["universe"]["status"], "fail")
        self.assertIsNone(c["universe"]["fix_command"])
        self.assertTrue(c["universe"]["ask_human"])
        self.assertIn("declined", c["universe"]["detail"])
        self.assertIn("open data pack", c["universe"]["detail"])
        self.assertNotIn("record_answer_commands", c["universe"])
        self.assertNotIn("record_answer_commands", c["consent_gray_sources"])
        self.assertIsNone(r["next_command"])                         # also with no OpenRouter key (a failed check)
        self.assertEqual(c["key_openrouter"]["status"], "fail")
        self.assertEqual(consent.get(self.cfg)["state"], "no")
        self.assertNotIn("next:", doctor.format_text(r))

    def test_declined_consent_with_stale_universe_never_points_at_consent(self) -> None:
        self.make_store(as_of=TODAY - dt.timedelta(days=40))
        keys.write_key(self.cfg, "openrouter", FAKE_OR)
        consent.record(self.cfg, "gray-sources", "no")
        r = doctor.run(self.cfg, today=TODAY)
        c = self.by_id(r)
        self.assertEqual(c["universe"]["status"], "warn")
        self.assertIsNone(c["universe"]["fix_command"])
        self.assertIn("declined", c["universe"]["detail"])
        self.assertIsNone(r["next_command"])
        self.assertFalse(r["ok"])

    def test_every_command_is_safe_to_run_verbatim_and_none_records_consent(self) -> None:
        """An agent runs next_command / fix_command as-is: no templates, no shell operators, never a consent write."""
        results = [doctor.run(self.cfg, today=TODAY)]                                     # no home
        results.append(doctor.run(self.cfg, today=TODAY, importer=lambda m: None))        # no duckdb
        with store.session(self.cfg):
            pass
        results.append(doctor.run(self.cfg, today=TODAY))                                 # empty, consent unset
        (self.home / "consent.json").write_text("{not json")
        results.append(doctor.run(self.cfg, today=TODAY))                                 # consent unreadable
        consent.record(self.cfg, "gray-sources", "no")
        results.append(doctor.run(self.cfg, today=TODAY))                                 # empty, declined
        consent.record(self.cfg, "gray-sources", "yes")
        results.append(doctor.run(self.cfg, today=TODAY))                                 # empty, yes
        with store.session(self.cfg) as con:
            _populate(con, as_of=TODAY - dt.timedelta(days=40), with_desc=False, with_doc=False)
        (self.home / "openrouter_api_key").write_text("")
        results.append(doctor.run(self.cfg, today=TODAY))                                 # stale, blank key
        keys.write_key(self.cfg, "openrouter", FAKE_OR)
        os.chmod(self.home / "openrouter_api_key", 0o644)
        results.append(doctor.run(self.cfg, today=TODAY))                                 # loose key mode
        consent.record(self.cfg, "gray-sources", "no")
        results.append(doctor.run(self.cfg, today=TODAY))                                 # stale, declined
        (self.home / "consent.json").unlink()
        results.append(doctor.run(self.cfg, today=TODAY))                                 # stale, unset
        commands = [doctor.check_python((3, 9, 18))["fix_command"]]
        for r in results:
            commands.append(r["next_command"])
            for c in r["checks"]:
                commands.append(c["fix_command"])
                self.assertNotIn("|", c.get("human_question", ""))
                for cmd in c.get("record_answer_commands", []):
                    self.assert_shell_safe(cmd)
        commands = [cmd for cmd in commands if cmd is not None]
        self.assertGreater(len(commands), 20)
        for cmd in commands:
            self.assert_shell_safe(cmd)
            self.assertNotIn("consent set", cmd)

    def assert_shell_safe(self, cmd: str) -> None:
        import shlex
        for bad in ("`", "$(", "${", "\n"):
            self.assertNotIn(bad, cmd, cmd)
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
        self.assertTrue(tokens and tokens[0] in ("jevscreen", "python3", "chmod"), cmd)
        for tok in tokens:
            self.assertFalse(tok and set(tok) <= set("|&;<>()"), f"shell operator {tok!r} in {cmd!r}")

    def test_cooldown_marker_is_reported(self) -> None:
        from jevscreen import guard
        self.make_ready()
        guard.mark_blocked(self.cfg, "crawl-descriptions", url="https://example.invalid/x", status=429,
                           reason="http_429")
        c = self.by_id(doctor.run(self.cfg, today=TODAY))
        self.assertEqual(c["cooldown"]["status"], "warn")
        self.assertIn("crawl-descriptions", c["cooldown"]["cooldowns"])
        self.assertTrue(c["cooldown"]["ask_human"])

    def test_locked_store_skips_data_checks(self) -> None:
        import duckdb
        self.make_ready()
        with mock.patch.object(doctor, "STORE_WAIT_S", 0.05), \
                mock.patch.object(duckdb, "connect", side_effect=duckdb.IOException("Could not set lock on file")):
            r = doctor.run(self.cfg, today=TODAY)
        c = self.by_id(r)
        self.assertEqual(c["store"]["status"], "warn")
        self.assertEqual(c["universe"]["status"], "skip")
        self.assertTrue(r["ok"])

    def test_dependencies_and_python(self) -> None:
        r = doctor.run(self.cfg, today=TODAY, importer=lambda m: None)
        c = self.by_id(r)
        self.assertEqual(c["dep_duckdb"]["status"], "fail")
        self.assertEqual(c["dep_pymupdf"]["status"], "warn")
        self.assertEqual(c["store"]["status"], "skip")
        self.assertEqual(r["next_command"], "python3 -m pip install -e .")
        c = self.by_id(doctor.run(self.cfg, today=TODAY, importer=lambda m: "1.0" if m in ("duckdb", "pypdf") else None))
        self.assertIn("pypdf", c["dep_pymupdf"]["detail"])
        self.assertEqual(doctor.check_python((3, 9, 18))["status"], "fail")
        self.assertEqual(doctor.check_python((3, 10, 0))["status"], "ok")

    def test_cli_doctor_runs_without_duckdb(self) -> None:
        """The entry point must not import the store (duckdb) before doctor can report it missing."""
        with mock.patch.dict(sys.modules, {"jevscreen.store": None, "duckdb": None}), \
                mock.patch.object(doctor, "_import_version", lambda m: None):
            code, out, _ = self.run_cli("doctor", "--json")
        self.assertEqual(code, 1)
        c = self.by_id(json.loads(out))
        self.assertEqual(c["dep_duckdb"]["status"], "fail")
        self.assertEqual(c["store"]["status"], "skip")

    def test_declined_consent_blocks_readiness(self) -> None:
        self.make_ready()
        consent.record(self.cfg, "gray-sources", "no")
        r = doctor.run(self.cfg, today=TODAY)
        c = self.by_id(r)
        self.assertEqual(c["consent_gray_sources"]["status"], "fail")
        self.assertFalse(r["ok"])
        self.assertIsNone(r["next_command"])
        self.assertIn("consent_gray_sources", r["ask_human"])

    def test_empty_openrouter_file_fails(self) -> None:
        self.make_ready()
        (self.home / "openrouter_api_key").write_text("")
        c = self.by_id(doctor.run(self.cfg, today=TODAY))
        self.assertEqual(c["key_openrouter"]["status"], "fail")
        self.assertIn("empty", c["key_openrouter"]["detail"])

    def test_cli_doctor_creates_nothing(self) -> None:
        code, out, _ = self.run_cli("doctor", "--json")
        self.assertEqual(code, 1)
        self.assertFalse(self.home.exists())
        self.assertEqual(self.by_id(json.loads(out))["home"]["fix_command"], "jevscreen init")

    def test_doctor_leaves_a_populated_home_untouched(self) -> None:
        self.make_ready()
        import shutil
        shutil.rmtree(self.home / "raw")

        def tree() -> dict:
            return {str(p.relative_to(self.home)): (p.is_dir(), p.stat().st_size, p.stat().st_mtime_ns)
                    for p in sorted(self.home.rglob("*"))}
        before = tree()
        r = doctor.run(self.cfg, today=TODAY)
        self.assertTrue(r["ok"])
        code, _, _ = self.run_cli("doctor", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(tree(), before)
        self.assertFalse((self.home / "raw").exists())

    def test_dependency_probe_does_not_import(self) -> None:
        with mock.patch.dict(sys.modules):
            sys.modules.pop("fitz", None)
            self.assertIsNotNone(doctor._import_version("duckdb"))
            doctor._import_version("fitz")
            self.assertNotIn("fitz", sys.modules)
        self.assertIsNone(doctor._import_version("no_such_module_xyz"))

    def test_loose_key_file_mode_warns(self) -> None:
        self.make_ready()
        os.chmod(self.home / "openrouter_api_key", 0o644)
        c = self.by_id(doctor.run(self.cfg, today=TODAY))
        self.assertEqual(c["key_openrouter"]["status"], "warn")
        self.assertIn("chmod 600", c["key_openrouter"]["fix_command"])


class ScreenDefaultsDocTest(unittest.TestCase):
    def test_install_survives_a_fresh_shell_per_call(self) -> None:
        """Every command in the JSON starts with a bare `jevscreen`, and many agent harnesses start each command in a
        fresh shell (the venv activation is lost): each install block must say how every later call finds it."""
        text = (SRC.parent / "AGENTS.md").read_text()
        hint = 'export PATH="$PWD/.venv/bin:$PATH"'
        blocks = re.findall(r"```bash\n(.*?)```", text, re.S)
        installs = [b for b in blocks if ".venv" in b and "pip install" in b]
        self.assertGreaterEqual(len(installs), 2)
        for b in installs:
            self.assertIn(hint, b)
            self.assertNotIn(". .venv/bin/activate", b)
        flat = " ".join(text.split())
        self.assertIn("fresh shell", flat)

    def test_docs_state_the_screen_cli_defaults(self) -> None:
        """The $1 / $1B first-run defaults are a convention: the docs must say the CLI's own defaults differ."""
        d = cli.build_parser().parse_args(["screen", "idea"])
        budget, mcap = f"--budget {d.budget:g}", f"--min-mcap {d.min_mcap:.0e}".replace("e+0", "e")
        self.assertEqual((budget, mcap), ("--budget 3", "--min-mcap 2e8"))
        for doc in ("AGENTS.md", "docs/AGENT_API.md"):
            text = " ".join((SRC.parent / doc).read_text().split())   # ignore line wrapping
            self.assertIn(budget, text, doc)
            self.assertIn(mcap, text, doc)
            self.assertIn("always pass `--budget` and `--min-mcap`", text, doc)


class DoctorJevCheckTest(TempHome):
    def test_no_key_makes_no_request(self) -> None:
        opener = mock.Mock(side_effect=AssertionError("request sent"))
        self.assertEqual(doctor.check_jev(self.cfg, opener)["status"], "fail")
        opener.assert_not_called()

    def test_ok_request_shape(self) -> None:
        keys.write_key(self.cfg, "openrouter", FAKE_OR)
        seen = []

        def opener(req, timeout):
            seen.append(req)
            return FakeResponse(json.dumps({"data": {"limit_remaining": 7.5, "usage": 1.25,
                                                     "is_free_tier": False}}).encode())
        c = doctor.check_jev(self.cfg, opener)
        self.assertEqual(c["status"], "ok")
        self.assertEqual(c["limit_remaining"], 7.5)
        req = seen[0]
        self.assertEqual((req.full_url, req.get_method(), req.data), (doctor.JEV_CHECK_URL, "GET", None))
        self.assertEqual(req.get_header("Authorization"), "Bearer " + FAKE_OR)
        self.assertNotIn(FAKE_OR, json.dumps(c))

    def test_low_credit_warns_and_no_limit_is_ok(self) -> None:
        keys.write_key(self.cfg, "openrouter", FAKE_OR)
        low = lambda req, timeout: FakeResponse(b'{"data": {"limit_remaining": 0.4}}')  # noqa: E731
        self.assertEqual(doctor.check_jev(self.cfg, low)["status"], "warn")
        none = lambda req, timeout: FakeResponse(b'{"data": {"limit_remaining": null}}')  # noqa: E731
        c = doctor.check_jev(self.cfg, none)
        self.assertEqual(c["status"], "ok")
        self.assertIn("account balance is not checked", c["detail"])  # /api/v1/key does not return it

    def test_payment_required_means_the_human_must_add_credit(self) -> None:
        keys.write_key(self.cfg, "openrouter", FAKE_OR)

        def no_credit(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 402, "Payment Required", {}, None)
        c = doctor.check_jev(self.cfg, no_credit)
        self.assertEqual((c["status"], c["http_status"], c["ask_human"]), ("fail", 402, True))
        self.assertIsNone(c["fix_command"])
        self.assertIn("add credit", c["detail"])
        self.assertNotIn("try again later", c["detail"])

    def test_rejected_key_and_network_error(self) -> None:
        keys.write_key(self.cfg, "openrouter", FAKE_OR)

        def rejected(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
        c = doctor.check_jev(self.cfg, rejected)
        self.assertEqual((c["status"], c["http_status"]), ("fail", 401))

        def broken(req, timeout):
            raise urllib.error.URLError(f"bad header Bearer {FAKE_OR}")
        c = doctor.check_jev(self.cfg, broken)
        self.assertEqual(c["status"], "warn")
        self.assertNotIn(FAKE_OR, c["detail"])
        self.assertIn(doctor.KEY_PLACEHOLDER, c["detail"])

    def test_redirect_is_not_followed_by_the_default_opener(self) -> None:
        """urllib would resend the Authorization header to a redirect target; the default opener must not follow."""
        import http.client
        keys.write_key(self.cfg, "openrouter", FAKE_OR)
        opened = []

        class Redirecting(urllib.request.BaseHandler):
            handler_order = 100   # before the real HTTPSHandler

            def https_open(self, req):
                opened.append(req.full_url)
                msg = http.client.HTTPMessage()
                msg["Location"] = "https://elsewhere.invalid/steal"
                resp = urllib.response.addinfourl(io.BytesIO(b""), msg, req.full_url, 302)
                resp.msg = "Found"
                return resp
        real_build = urllib.request.build_opener
        no_net = mock.patch.object(http.client.HTTPConnection, "connect",
                                   side_effect=AssertionError("network access in a test"))
        with no_net, mock.patch.object(urllib.request, "build_opener", lambda *h: real_build(*h, Redirecting())):
            c = doctor.check_jev(self.cfg)
        self.assertEqual(opened, [doctor.JEV_CHECK_URL])
        self.assertEqual((c["status"], c["http_status"]), ("warn", 302))

    def test_odd_json_bodies(self) -> None:
        keys.write_key(self.cfg, "openrouter", FAKE_OR)
        for body in (b"[1, 2]", b"not json", b'{"data": [1]}'):
            c = doctor.check_jev(self.cfg, lambda req, timeout, b=body: FakeResponse(b))
            self.assertEqual(c["status"], "ok", body)

    def test_run_flag_uses_the_opener(self) -> None:
        keys.write_key(self.cfg, "openrouter", FAKE_OR)
        opener = mock.Mock(return_value=FakeResponse(b'{"data": {}}'))
        r = doctor.run(self.cfg, check_jev_flag=True, today=TODAY, jev_opener=opener)
        self.assertEqual(opener.call_count, 1)
        self.assertEqual({c["id"]: c for c in r["checks"]}["jev"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
