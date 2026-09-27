"""Tests for the daily pack job: jevscreen.pack_ci (tag, prune, publish gate) and offline checks of
ci/daily-pack.yml (copied to .github/workflows/ to enable). No network; manifests are synthetic."""
from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import pack, pack_ci  # noqa: E402

WORKFLOW = ROOT / "ci" / "daily-pack.yml"


def manifest(**rows: int) -> dict:
    return {"files": [{"name": f"{t}.jsonl.gz", "table": t, "rows": n} for t, n in rows.items()]
            + [{"name": "ATTRIBUTION.txt", "table": None, "rows": None}]}


def run_main(argv, stdin: str = "") -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with mock.patch("sys.stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        code = pack_ci.main(argv)
    return code, out.getvalue(), err.getvalue()


class TagTest(unittest.TestCase):
    def test_same_date_never_reuses_a_published_tag(self):
        self.assertEqual(pack_ci.release_tag("2026-10-03", []), "pack-2026-10-03")
        self.assertEqual(pack_ci.release_tag("2026-10-03", ["pack-2026-10-03", "v0.1.0"]), "pack-2026-10-03.2")
        self.assertEqual(pack_ci.release_tag("2026-10-03", ["pack-2026-10-03", "pack-2026-10-03.2"]),
                         "pack-2026-10-03.3")
        with self.assertRaises(ValueError):
            pack_ci.release_tag("2026-10-03; rm -rf /", [])

    def test_order(self):
        tags = ["pack-2026-10-03.10", "pack-2026-10-03", "v0.1.0", "pack-2026-10-04", "pack-2026-10-03.2", ""]
        self.assertEqual(pack_ci.pack_tags(tags),
                         ["pack-2026-10-04", "pack-2026-10-03.10", "pack-2026-10-03.2", "pack-2026-10-03"])
        code, out, _ = run_main(["newest"], "\n".join(tags))
        self.assertEqual((code, out), (0, "pack-2026-10-04\n"))
        code, out, _ = run_main(["tag", "--date", "2026-10-04"], "\n".join(tags))
        self.assertEqual(out, "pack-2026-10-04.2\n")

    def test_pull_picks_the_newest_run_of_a_day(self):
        class Fetch:
            def json(self, url):
                return [{"tag_name": t, "draft": False, "prerelease": False}
                        for t in ("pack-2026-10-03.2", "pack-2026-10-03.10", "pack-2026-10-03", "v9")]
        self.assertEqual(pack.find_release(Fetch(), "o/r")["tag_name"], "pack-2026-10-03.10")


class PruneTest(unittest.TestCase):
    TAGS = ["pack-2026-09-27", "pack-2026-09-26", "pack-2026-09-25", "pack-2026-09-24.2", "pack-2026-09-24",
            "v0.1.0", "pack-notes"]

    def test_keep_values(self):
        self.assertEqual(pack_ci.tags_to_prune(self.TAGS, "2"),
                         ["pack-2026-09-25", "pack-2026-09-24.2", "pack-2026-09-24"])
        self.assertEqual(pack_ci.tags_to_prune(self.TAGS, "7"), [])
        # typos never delete the newest pack: non-numbers mean the default, and at least one is kept
        for bad in ("0", "abc", " ", "", "-1", "7d", "a[$(echo INJECTED >&2)]", "３", None):
            kept = [t for t in pack_ci.pack_tags(self.TAGS) if t not in pack_ci.tags_to_prune(self.TAGS, bad)]
            self.assertIn("pack-2026-09-27", kept, bad)
        self.assertEqual(pack_ci.parse_keep("0"), 1)
        self.assertEqual(pack_ci.parse_keep("abc"), pack_ci.DEFAULT_KEEP)
        self.assertEqual(pack_ci.tags_to_prune(self.TAGS, "0"),
                         ["pack-2026-09-26", "pack-2026-09-25", "pack-2026-09-24.2", "pack-2026-09-24"])

    def test_cli_prints_nothing_but_pack_tags(self):
        code, out, _ = run_main(["prune", "--keep=-1"], "\n".join(self.TAGS))
        self.assertEqual(code, 0)
        self.assertNotIn("pack-2026-09-27", out)          # -1 means 1: the newest stays
        self.assertNotIn("v0.1.0", out)


class GateTest(unittest.TestCase):
    def test_requires_data(self):
        self.assertEqual(pack_ci.gate(manifest(), None), (False, ["the pack holds no data rows"]))
        self.assertTrue(pack_ci.gate(manifest(edinet_codes=5), None)[0])

    def test_refuses_a_shrink_below_95_percent(self):
        self.assertEqual(pack_ci.MIN_RATIO, 0.95)
        prev = manifest(sec_tickers=10000, edinet_codes=5000, edinet_business=3800)
        ok, reasons = pack_ci.gate(manifest(sec_tickers=10000, edinet_codes=5000, edinet_business=3610), prev)
        self.assertTrue(ok, reasons)                                   # exactly 95 %
        ok, reasons = pack_ci.gate(manifest(sec_tickers=10000, edinet_codes=5000, edinet_business=3609), prev)
        self.assertFalse(ok)
        self.assertIn("edinet_business", reasons[0])
        ok, reasons = pack_ci.gate(manifest(sec_tickers=10000, edinet_codes=5000), prev)   # table missing
        self.assertFalse(ok)
        ok, _ = pack_ci.gate(manifest(sec_tickers=10000, edinet_codes=5000, edinet_business=300), prev,
                             allow_shrink=True)
        self.assertTrue(ok)
        self.assertFalse(pack_ci.gate(manifest(sec_tickers=1, edinet_codes=6000, edinet_business=4000), prev)[0])

    def test_cli(self):
        with tempfile.TemporaryDirectory() as t:
            new, old = Path(t) / "new.json", Path(t) / "old.json"
            new.write_text(json.dumps(manifest(edinet_business=100)))
            old.write_text(json.dumps(manifest(edinet_business=3800)))
            code, _, err = run_main(["gate", "--manifest", str(new), "--previous", str(old)])
            self.assertEqual(code, 1)
            self.assertIn("not publishing", err)
            self.assertEqual(run_main(["gate", "--manifest", str(new)])[0], 0)
            self.assertEqual(run_main(["gate", "--manifest", str(new), "--previous", str(old),
                                       "--allow-shrink"])[0], 0)


class WorkflowTest(unittest.TestCase):
    """Offline checks of the workflow text (the job itself cannot run in tests)."""

    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.steps = {}
        for block in re.split(r"\n      - (?=name:|uses:)", cls.text)[1:]:
            m = re.match(r"name: (.+)", block)
            cls.steps[m.group(1).strip() if m else block.splitlines()[0]] = block

    def step(self, prefix: str) -> str:
        hits = [b for n, b in self.steps.items() if n.startswith(prefix)]
        self.assertEqual(len(hits), 1, f"{prefix}: {list(self.steps)}")
        return hits[0]

    def test_tag_date_is_fixed_at_job_start(self):
        names = list(self.steps)
        date_i = next(i for i, n in enumerate(names) if n.startswith("Pack date"))
        sync_i = next(i for i, n in enumerate(names) if n.startswith("EDINET"))
        self.assertLess(date_i, sync_i)
        self.assertIn("date -u +%F", self.step("Pack date"))
        self.assertNotIn("date -u", self.step("Build pack"))
        self.assertIn("jevscreen.pack_ci tag", self.step("Build pack"))

    def test_published_pack_is_never_clobbered(self):
        self.assertNotIn("--clobber", self.text)
        self.assertNotIn("gh release upload", self.text)

    def test_keep_is_never_shell_arithmetic(self):
        self.assertNotIn("$((", self.text)
        self.assertIn("jevscreen.pack_ci prune --keep=", self.step("Keep the newest"))

    def test_build_scans_for_the_edinet_key(self):
        self.assertIn("JEVSCREEN_EDINET_API_KEY: ${{ secrets.EDINET_API_KEY }}", self.step("Build pack"))

    def test_publish_gate_and_bootstrap(self):
        build = self.step("Build pack")
        self.assertIn("jevscreen.pack_ci gate", build)
        self.assertIn('MIN_RATIO: "0.95"', build)
        boot = self.step("Bootstrap")
        self.assertIn("steps.restore.outputs.cache-matched-key == ''", boot)
        self.assertIn("jevscreen pack pull --repo", boot)

    def test_codelist_downloaded_once(self):
        self.assertIn("JEVSCREEN_EDINET_REUSE_CODELIST_HOURS", self.step("EDINET"))


if __name__ == "__main__":
    unittest.main()
