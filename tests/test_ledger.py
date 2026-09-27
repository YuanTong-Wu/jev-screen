"""Tests for the run ledger funnel.jsonl.gz written by every screen with an output directory."""
from __future__ import annotations

import gzip
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import keywords, screen  # noqa: E402
from test_screen import StoreCase, humanoid_sieve  # noqa: E402
from why_seed import LINES, seed_why  # noqa: E402


class TestLedger(StoreCase):
    def setUp(self):
        super().setUp()
        seed_why(self.cfg, self.home)

    def ledger(self, out="out"):
        got = screen.read_ledger(self.home / out)
        self.assertIsNotNone(got)
        return got[0], {ln["id"]: ln for ln in got[1]}

    def test_ok_run(self):
        res = self.run_screen(reads=1)
        h, by = self.ledger()
        self.assertEqual(h["format"], "jevscreen.funnel/1")
        self.assertEqual((h["run_id"], h["status"], h["dry_run"]), (res["run_id"], "ok", False))
        self.assertEqual(h["idea_key"], keywords.idea_key("humanoid robots"))
        self.assertEqual(h["market_as_of"], "2026-09-26")
        self.assertEqual(h["params"]["min_mcap_usd"], 2e8)
        self.assertEqual(h["params"]["shells"], "drop")
        self.assertEqual(h["shells_version"], 1)
        self.assertEqual(h["st_list"]["stale"], False)
        self.assertEqual(screen.read_ledger_header(self.home / "out"), h)
        # one line per universe company, each with its stage
        self.assertEqual(len(by), 7 + len(LINES))
        self.assertEqual(by["NYSE:TINY"]["s"], "below_min_mcap")
        self.assertEqual(by["NYSE:TINY"]["m"], 100000000)
        self.assertIsInstance(by["NYSE:ROBO"]["m"], int)
        self.assertEqual(by["NYSE:NODS"]["s"], "no_description")
        self.assertEqual(by["NASDAQ:FXAC"]["s"], "shell")
        self.assertEqual(by["NASDAQ:FXAC"]["r"], ["spac"])
        self.assertEqual(by["NASDAQ:FXAC"]["e"]["spac"]["pat"], "name_acq")
        self.assertEqual(by["NYSE:KPAC"]["f"], ["spac_like"])
        self.assertEqual(by["SZSE:309901"]["f"], ["st"])
        robo = by["NYSE:ROBO"]
        self.assertEqual(robo["s"], "l1_sent")
        self.assertEqual(robo["l1"]["lab"], "core")
        self.assertEqual(robo["l1"]["p"], [0.9, 0.05, 0.03, 0.02])
        self.assertTrue(robo["l1"]["ok"])
        self.assertEqual(robo["l2"]["lab"], "explicit")
        self.assertEqual(robo["l2"]["ev"], "annual_report")
        bank = by["NASDAQ:BANK"]
        self.assertEqual((bank["s"], bank["l1"]["lab"], bank["l1"]["ok"]), ("l1_sent", "unrelated", False))
        self.assertNotIn("l2", bank)
        # never the description text (pattern id + offsets only)
        raw = gzip.decompress((self.home / "out" / "funnel.jsonl.gz").read_bytes()).decode("utf-8")
        for _sid, _n, _i, _c, _ind, _m, _r, text in LINES:
            self.assertNotIn(text[:40], raw)
        self.assertNotIn("blank check", raw)

    def test_dry_run_and_budget(self):
        res = self.run_screen(reads=1, dry_run=True)
        h, by = self.ledger()
        self.assertEqual((h["status"], h["dry_run"], h["run_id"]), ("dry_run", True, res["run_id"]))
        self.assertEqual(by["NYSE:ROBO"]["s"], "l1_not_sent")
        self.assertNotIn("l1", by["NYSE:ROBO"])
        res = self.run_screen(reads=1, budget_usd=0.01, out_dir=self.home / "b")
        self.assertEqual(res["status"], "budget_exhausted")
        h, by = self.ledger("b")
        self.assertEqual(h["status"], "budget_exhausted")
        stages = {ln["s"] for ln in by.values()}
        self.assertIn("l1_not_sent", stages)
        self.assertTrue(any(ln.get("l1", {}).get("st") == "skipped_budget" for ln in by.values()))

    def test_from_run_and_extra(self):
        res = self.run_screen(reads=1)
        sv = humanoid_sieve(examples=[{"security_id": "NYSE:TINY", "want": "explicit", "source": "sieve"}])
        self.run_screen(reads=1, from_run=res["run_id"], sieve=sv, out_dir=self.home / "o2")
        _h, by = self.ledger("o2")
        self.assertEqual(by["NYSE:ROBO"]["s"], "l1_loaded")
        self.assertEqual(by["NYSE:TINY"]["x"], "below_min_mcap")
        self.assertEqual(by["NYSE:TINY"]["l2"]["lab"], "partial")

    def test_missing_ledger(self):
        self.assertIsNone(screen.read_ledger(self.home / "nothing"))
        self.assertIsNone(screen.read_ledger_header(self.home / "nothing"))


if __name__ == "__main__":
    unittest.main()
