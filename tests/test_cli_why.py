"""CLI tests for `jevscreen why`, the sieve command wiring and docs/sieve.schema.json."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import cli, screen, sieve_author, why_cli  # noqa: E402
from test_screen import StoreCase, make_factory  # noqa: E402
from why_seed import seed_why  # noqa: E402

SCHEMA = Path(__file__).resolve().parents[1] / "docs" / "sieve.schema.json"


class TestWiring(unittest.TestCase):
    def test_commands(self):
        self.assertTrue({"why", "screen", "sieve", "doctor"} <= set(cli.COMMANDS))
        self.assertEqual(set(why_cli.COMMANDS), {"why"})          # test_cli_coverage checks the rest exactly
        self.assertTrue({"new", "set", "add", "remove", "pin", "unpin", "list", "check"} <= set(why_cli.SIEVE_COMMANDS))
        a = cli.build_parser().parse_args(["why", "2330", "示信", "--run", "latest", "--lang", "zh", "--json"])
        self.assertEqual((a.targets, a.run, a.lang, a.json, a.wait), (["2330", "示信"], "latest", "zh", True, 5.0))
        a = cli.build_parser().parse_args(["sieve", "check", "--key", "0123456789abcdef", "--json"])
        self.assertEqual((a.target, a.key, a.json), (None, "0123456789abcdef", True))
        a = cli.build_parser().parse_args(["screen", "--from-run", "scr-1", "--shells", "keep"])
        self.assertEqual((a.idea, a.shells, a.given), (None, "keep", frozenset({"shells"})))

    def test_schema_matches_author_fields(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(set(schema["properties"]), set(sieve_author.AUTHOR_FIELDS) | {"format"})
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["idea_en"]["maxLength"], sieve_author.IDEA_EN_MAX)
        self.assertEqual(schema["properties"]["should_pass"]["maxItems"], sieve_author.CHECK_MAX)
        seed = schema["properties"]["seed_terms"]
        self.assertEqual(set(seed["properties"]), set(sieve_author.LANGS))
        self.assertEqual(seed["properties"]["en"], {"$ref": "#/$defs/terms"})
        self.assertEqual(schema["$defs"]["terms"]["maxItems"], sieve_author.SEED_MAX)
        self.assertEqual(schema["$defs"]["terms"]["items"]["maxLength"], sieve_author.SEED_CHARS)
        for ex in schema.get("examples") or []:
            self.assertEqual(sieve_author.validate_draft(ex), [], ex)
        try:
            import jsonschema
        except ImportError:
            return
        for ex in schema.get("examples") or []:
            jsonschema.validate(ex, schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate({"include": ["X"]}, schema)


class TestWhyCli(StoreCase):
    def setUp(self):
        super().setUp()
        seed_why(self.cfg, self.home)

    def main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"JEVSCREEN_HOME": str(self.home)}), \
                mock.patch.object(screen, "_default_factory", make_factory(self.log)), \
                mock.patch.object(screen, "_default_keywords", self.kw), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_no_real_ondemand_child_in_tests(self):
        """Plain `screen` defaults to --fetch-docs auto and the why seed has profile-only A-shares (CNINFO route):
        the suite-wide kill switch must refuse the real adapters, so no `jevscreen.ondemand child` ever starts."""
        from jevscreen import ondemand
        self.assertEqual(os.environ.get("JEVSCREEN_TESTING"), "1")
        launched = []

        def popen(argv, *a, **kw):
            launched.append(list(argv))
            raise OSError("test: no child")
        with mock.patch.object(ondemand.subprocess, "Popen", popen):
            code, _, _ = self.main(["screen", "humanoid robots", "--reads", "1", "--budget", "1"])
        self.assertEqual(code, 0)
        self.assertEqual([a for a in launched if "jevscreen.ondemand" in a], [])

    def test_exit_codes_and_output(self):
        code, out, err = self.main(["why", "NYSE:ROBO"])
        self.assertEqual(code, 1)
        self.assertIn("还没运行过筛选" if "还没" in err else "No screen", err)
        self.main(["screen", "humanoid robots", "--reads", "1", "--budget", "1", "--fetch-docs", "off"])
        code, out, _ = self.main(["why", "NASDAQ:BANK", "--lang", "en"])
        self.assertEqual(code, 0)
        self.assertIn("One line: The model read only its profile", out)
        code, out, _ = self.main(["why", "NASDAQ:BANK", "Nonexistent Widgets", "--json"])
        self.assertEqual(code, 1)
        body = json.loads(out)
        self.assertEqual(body["status"], "partly_resolved")
        self.assertEqual(body["results"][1]["agent_hint_en"][:28], "Resolve the name to an excha")
        code, out, _ = self.main(["why", "NYSE:ROBO", "--json", "--show-text"])
        body = json.loads(out)
        self.assertEqual((code, body["results"][0]["stop"]), (0, "in_output"))
        code, out, _ = self.main(["why", "NASDAQ:ROB2", "--json", "--show-text"])
        quotes = [f for f in json.loads(out)["results"][0]["facts"] if f.get("tier")]
        self.assertEqual(quotes[0]["tier"], "official-private")
        self.assertLessEqual(len(quotes[0]["en"].split(": ", 1)[1]), 200)
        code, out, _ = self.main(["why", "--json"])
        self.assertEqual((code, json.loads(out)["status"]), (1, "no_targets"))
        code, out, _ = self.main(["why", "NYSE:ROBO", "--run", "scr-nope", "--json"])
        self.assertEqual((code, json.loads(out)["status"]), (1, "unknown_run"))


if __name__ == "__main__":
    unittest.main()
