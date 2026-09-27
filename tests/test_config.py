"""Config lookups for secrets and optional local files: env first, then git-ignored files under <home>, never a
machine-specific fallback path. All values here are fake."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import config  # noqa: E402
from jevscreen.config import Config  # noqa: E402

FAKE = "sk-or-v1-FAKE-config-test-0000"
CLEAN = {"OPENROUTER_API_KEY": "", "JEVSCREEN_OPENROUTER_KEY_FILE": "", "JEVSCREEN_FD_DUCKDB": ""}


class OpenRouterKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, CLEAN)
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_none_without_env_or_file(self):
        self.assertIsNone(Config(home=self.home).openrouter_key())

    def test_home_file(self):
        (self.home / "openrouter_api_key").write_text(FAKE + "\n")
        self.assertEqual(Config(home=self.home).openrouter_key(), FAKE)

    def test_named_file_beats_home_file(self):
        (self.home / "openrouter_api_key").write_text("home-key")
        named = self.home / "elsewhere"
        named.write_text(FAKE)
        with mock.patch.dict(os.environ, {"JEVSCREEN_OPENROUTER_KEY_FILE": str(named)}):
            self.assertEqual(Config(home=self.home).openrouter_key(), FAKE)
        with mock.patch.dict(os.environ, {"JEVSCREEN_OPENROUTER_KEY_FILE": str(self.home / "missing")}):
            self.assertIsNone(Config(home=self.home).openrouter_key())   # named but missing: no silent fallback

    def test_env_beats_files(self):
        (self.home / "openrouter_api_key").write_text("home-key")
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": f"  {FAKE} "}):
            self.assertEqual(Config(home=self.home).openrouter_key(), FAKE)

    def test_key_source_names_where_it_looked(self):
        cfg = Config(home=self.home)
        self.assertEqual(cfg.openrouter_key_source(), ("home-file", self.home / "openrouter_api_key"))
        with mock.patch.dict(os.environ, {"JEVSCREEN_OPENROUTER_KEY_FILE": str(self.home / "named")}):
            self.assertEqual(cfg.openrouter_key_source(), ("named-file", self.home / "named"))
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": FAKE}):
            self.assertEqual(cfg.openrouter_key_source(), ("env", None))

    def test_missing_key_hint_names_the_path_and_env_vars(self):
        hint = Config(home=self.home).openrouter_key_hint()
        self.assertIn(str(self.home / "openrouter_api_key"), hint)
        self.assertIn("OPENROUTER_API_KEY", hint)
        self.assertIn("JEVSCREEN_OPENROUTER_KEY_FILE", hint)
        named = self.home / "missing"
        with mock.patch.dict(os.environ, {"JEVSCREEN_OPENROUTER_KEY_FILE": str(named)}):
            hint = Config(home=self.home).openrouter_key_hint()
        self.assertIn(str(named), hint)
        self.assertIn("not checked", hint)                               # the home file is not a fallback
        (self.home / "openrouter_api_key").write_text(FAKE)
        self.assertNotIn(FAKE, Config(home=self.home).openrouter_key_hint())

    def test_no_machine_specific_defaults(self):
        src = Path(config.__file__).read_text(encoding="utf-8")
        self.assertNotIn("/Users/", src)
        self.assertNotIn(".secrets", src)


class FdDuckdbPathTests(unittest.TestCase):
    def test_default_follows_home(self):
        with mock.patch.dict(os.environ, CLEAN):
            self.assertEqual(Config(home=Path("/tmp/jh")).fd_duckdb, Path("/tmp/jh") / "financedatabase.duckdb")

    def test_env_and_explicit(self):
        with mock.patch.dict(os.environ, {"JEVSCREEN_FD_DUCKDB": "/tmp/fd.duckdb"}):
            self.assertEqual(Config(home=Path("/tmp/jh")).fd_duckdb, Path("/tmp/fd.duckdb"))
        self.assertEqual(Config(home=Path("/tmp/jh"), fd_duckdb=Path("/x.duckdb")).fd_duckdb, Path("/x.duckdb"))


if __name__ == "__main__":
    unittest.main()
