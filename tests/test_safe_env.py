"""The suite-wide network kill switch (tests/safe_env.py) covers every test module."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import safe_env  # noqa: E402

HERE = Path(__file__).resolve().parent


class TestSafeEnv(unittest.TestCase):
    def test_every_test_module_imports_it(self):
        missing = [p.name for p in sorted(HERE.glob("test_*.py"))
                   if "import safe_env" not in p.read_text(encoding="utf-8")]
        self.assertEqual(missing, [])

    def test_apply_sets_testing_and_drops_keys(self):
        env = {k: "x" for k in safe_env.KEY_ENVS}
        env["JEVSCREEN_TESTING"] = "0"
        safe_env.apply(env)
        self.assertEqual(env, {"JEVSCREEN_TESTING": "1"})
        self.assertEqual(os.environ.get("JEVSCREEN_TESTING"), "1")
        with mock.patch.dict(os.environ, {"JEVSCREEN_SEC_USER_AGENT": "Test Person test@example.org"}):
            from jevscreen import ondemand
            with self.assertRaises(ondemand.RealAdaptersRefused):
                ondemand.adapter_module("sec")


if __name__ == "__main__":
    unittest.main()
