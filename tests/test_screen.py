"""Tests for jevscreen.screen, jevscreen.report and the `screen` CLI command.

No network and no paid calls: every Jev client is a fake passed through `jev_factory` (or patched in as the default
factory for CLI tests). The store is a temp DuckDB seeded with store helpers.
"""
from __future__ import annotations

import contextlib
import csv
import datetime as dt
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

try:  # see test_cli_coverage: load numpy before any mock.patch.dict(sys.modules) could drop it
    import numpy  # noqa: F401
except ImportError:
    pass

from jevscreen import cli, config, report, screen, store  # noqa: E402

D = dt.date(2026, 9, 26)
SEC_COLS = ("security_id", "exchange", "symbol", "name", "isin", "country", "tv_type", "tv_subtype", "is_primary",
            "company_key", "last_seen_snapshot", "active")

SEC_TEXT_ROBO = """Item 1. Business

Overview

RoboCorp designs and manufactures industrial automation equipment for factories in North America, Europe and Asia.
Our products are sold to automotive, electronics and logistics customers through direct sales teams.

Products

Humanoid Robots

We develop and sell humanoid robots for warehouse picking and assembly work. Our humanoid robots are deployed at
customer sites under multi-year service contracts and are our fastest growing product line.

Competition

We compete with large industrial conglomerates and with specialised automation vendors on price and reliability.

Employees

As of year end we had approximately 4,000 full-time employees located in eleven countries around the world.
"""

SEC_TEXT_ROB2 = """Item 1. Business

Robo Two provides motion control software and robot controllers used by machine builders and system integrators.

Our controller platform runs on standard industrial PCs and supports third-party robot arms from many vendors.

We sell licences and maintenance subscriptions and we do not manufacture robots ourselves at this time.

Human capital: we employ about 900 people, most of them software engineers based in our two development centres.
"""


class FakeBudgetExceeded(RuntimeError):
    pass


FakeBudgetExceeded.__name__ = "BudgetExceeded"


class JevUnavailable(RuntimeError):   # matched by class name, like jev.JevUnavailable
    pass


def l1_label(text: str) -> tuple[str, dict[str, float]]:
    t = text.lower()
    if "robot" in t:
        return "core", {"core": 0.9, "adjacent": 0.05, "unrelated": 0.03, "insufficient": 0.02}
    if "servo" in t:
        return "adjacent", {"core": 0.35, "adjacent": 0.4, "unrelated": 0.2, "insufficient": 0.05}
    if "gearbox" in t:
        return "adjacent", {"core": 0.1, "adjacent": 0.3, "unrelated": 0.5, "insufficient": 0.1}
    return "unrelated", {"core": 0.01, "adjacent": 0.04, "unrelated": 0.9, "insufficient": 0.05}


def l2_label(text: str) -> tuple[str, dict[str, float]]:
    t = text.lower()
    if "humanoid robots" in t or any(w in text for w in ("人形机器人", "ヒューマノイド", "휴머노이드")):
        return "explicit", {"explicit": 0.8, "partial": 0.1, "contradicted": 0.05, "insufficient": 0.05}
    if "robot" in t:
        return "partial", {"explicit": 0.2, "partial": 0.6, "contradicted": 0.1, "insufficient": 0.1}
    return "insufficient", {"explicit": 0.05, "partial": 0.15, "contradicted": 0.1, "insufficient": 0.7}


def facet_label(question, text: str) -> tuple[str, dict[str, float]]:
    """Default facet answer of the fakes (question keys facet_role / facet_scope / facet_geo): the family's keep label
    (the first criterion) with 0.85, unless FACET_KEYWORDS names another label for a word of the text."""
    labels = list(question.criteria)
    fam = question.key[len("facet_"):]
    t = text.lower()
    for word, fam_w, label, p in FACET_KEYWORDS:
        if fam_w == fam and word in t and label in labels:
            rest = [x for x in labels if x != label]
            return label, {label: p, rest[0]: round(1 - p, 4)}
    return labels[0], {labels[0]: 0.85, labels[-1]: 0.15}


# (word in the L2 text, family, label, probability): the fixtures' facet answers (e.g. "pos" -> hardware)
FACET_KEYWORDS: list[tuple[str, str, str, float]] = [("pos terminal", "role", "hardware", 0.9),
                                                      ("gearbox", "role", "upstream", 0.9)]


class FakeJev:
    """Deterministic JevClient stand-in: labels by keyword, $0.01 per request of `pack_size` items. Facet questions
    (facet_*) are answered by facet_label."""
    COST = 0.01

    def __init__(self, cfg, *, run_id, layer, budget_usd, dry_run=False, pack_size=8, log=None):
        self.run_id, self.layer, self.budget_usd, self.dry_run, self.pack_size = run_id, layer, budget_usd, dry_run, \
            pack_size
        self._spent, self._sent = 0.0, 0
        self.classified: list = []
        self.estimated: list = []
        if log is not None:
            log.append(self)

    @property
    def spent_usd(self):
        return self._spent

    @property
    def requests_sent(self):
        return self._sent

    def estimate(self, items, question):
        self.estimated.append((len(items), question.key))
        n = -(-len(items) // self.pack_size)
        return {"requests": n, "items": len(items), "est_input_tokens": sum(len(i.text) for i in items) // 4,
                "est_cost_usd": n * self.COST, "basis": "fake"}

    def estimate_uncached(self, items, question):
        return self.estimate(items, question)      # the fake keeps no answer cache: every item is priced

    def classify(self, items, question):
        assert not self.dry_run, "dry run must not classify"
        self.classified.append((list(items), question))
        if question.key.startswith("facet_"):
            def fn(text, _q=question):
                return facet_label(_q, text)
        else:
            fn = l1_label if question.key == "fit" else l2_label
        out = []
        for i in range(0, len(items), self.pack_size):
            pack = items[i:i + self.pack_size]
            if self._spent + self.COST > self.budget_usd + 1e-12:
                out += [{"item_id": it.item_id, "label": None, "probs": {}, "request_id": None,
                         "status": "skipped_budget", "error": "budget", "cached": False} for it in pack]
                continue
            self._spent += self.COST
            self._sent += 1
            rid = f"{self.layer}-req-{self._sent}"
            for it in pack:
                label, probs = fn(it.text)
                out.append({"item_id": it.item_id, "label": label, "probs": probs, "request_id": rid,
                            "status": "ok", "error": None, "cached": False})
        return out


def make_factory(log, **kw):
    def factory(cfg, *, run_id, layer, budget_usd, dry_run):
        return FakeJev(cfg, run_id=run_id, layer=layer, budget_usd=budget_usd, dry_run=dry_run, log=log, **kw)
    return factory


def seed(cfg: config.Config, home: Path) -> None:
    docs = home / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "robo.txt").write_text(SEC_TEXT_ROBO, encoding="utf-8")
    (docs / "rob2.txt").write_text(SEC_TEXT_ROB2, encoding="utf-8")
    with store.session(cfg) as con:
        snap = lambda src: store.record_snapshot(  # noqa: E731
            con, source_id=src, kind="t", request=None, raw_path=None, raw_sha256=None, raw_bytes=None, rows=None,
            duration_s=None)
        scan, prof, fd, sec = (snap("tradingview_scanner"), snap("tradingview_profile"),
                               snap("financedatabase_local"), snap("sec_filing_text"))

        def line(sid, name, isin, country):
            ex, sym = sid.split(":")
            return (sid, ex, sym, name, isin, country, "stock", "common", True, store.company_key(isin, sid), scan,
                    True)

        store.upsert_many(con, "securities", SEC_COLS, [
            line("NYSE:ROBO", "RoboCorp", "US0000000001", "United States"),
            line("TSE:6000", "ServoJP", "JP0000000002", "Japan"),
            line("LSE:GEAR", "GearCo", "GB0000000003", "United Kingdom"),
            line("NASDAQ:BANK", "BankCo", "US0000000004", "United States"),
            line("NYSE:NODS", "NoDesc", "US0000000005", "United States"),
            line("NYSE:TINY", "Tiny Robots", "US0000000006", "United States"),
            line("NASDAQ:ROB2", "Robo Two", "US0000000007", "United States"),
        ])
        mcols = ("security_id", "as_of", "market_cap_usd", "avg_volume_10d", "snapshot_id")
        store.upsert_many(con, "market_daily", mcols, [
            ("NYSE:ROBO", D, 5e9, 1e6, scan), ("TSE:6000", D, 1e9, 5e4, scan), ("LSE:GEAR", D, 3e9, 2e6, scan),
            ("NASDAQ:BANK", D, 8e9, 3e6, scan), ("NYSE:NODS", D, 2e9, 1e6, scan), ("NYSE:TINY", D, 1e8, 1e6, scan),
            ("NASDAQ:ROB2", D, 4e9, 1e6, scan)])
        dcols = ("security_id", "source_id", "company_key", "text", "snapshot_id")
        store.upsert_many(con, "descriptions", dcols, [
            ("NYSE:ROBO", "tradingview_profile", "isin:US0000000001", "RoboCorp makes industrial robot arms.", prof),
            ("NYSE:ROBO", "financedatabase_local", "isin:US0000000001",
             "RoboCorp Inc. designs automation equipment and humanoid machines for warehouses.", fd),
            ("TSE:6000", "financedatabase_local", "isin:JP0000000002",
             "ServoJP manufactures servo motors for factory automation.", fd),
            ("LSE:GEAR", "tradingview_profile", "isin:GB0000000003", "GearCo makes gearbox units for trucks.", prof),
            ("NASDAQ:BANK", "tradingview_profile", "isin:US0000000004", "BankCo offers retail banking.", prof),
            ("NYSE:TINY", "tradingview_profile", "isin:US0000000006", "Tiny makes robot toys.", prof),
            ("NASDAQ:ROB2", "sec_filing_text", "isin:US0000000007",
             "Robo Two provides motion control software and robot controllers.", sec),
        ])
        store.upsert_many(con, "identifiers", ("security_id", "id_type", "id_value", "method", "snapshot_id"), [
            ("NASDAQ:ROB2", "sec_cik", "777", "test", sec)])
        dccols = ("doc_id", "security_id", "company_key", "source_id", "cik", "form", "section", "filing_date", "url",
                  "text_path", "snapshot_id")
        store.upsert_many(con, "documents", dccols, [
            ("sec_filing_text:111:a:item1", "NYSE:ROBO", "isin:US0000000001", "sec_filing_text", "111", "10-K",
             "item1", dt.date(2026, 2, 1), "https://www.sec.gov/robo.htm", str(docs / "robo.txt"), sec),
            # ROB2's document is attached only through identifiers.sec_cik -> documents.cik
            ("sec_filing_text:777:b:item1", None, None, "sec_filing_text", "777", "10-K", "item1",
             dt.date(2026, 3, 1), "https://www.sec.gov/rob2.htm", str(docs / "rob2.txt"), sec),
        ])
        fcols = ("security_id", "as_of", "metric", "period", "value_usd", "value_local", "currency_local",
                 "snapshot_id")
        store.upsert_many(con, "fundamentals_current", fcols, [
            ("NYSE:ROBO", D, "total_revenue", "ttm", 1.5e9, 1.5e9, "USD", scan),
            ("TSE:6000", D, "total_revenue", "ttm", 9.0e6, 1.3e9, "JPY", scan),
            ("TSE:6000", D, "total_revenue", "fy", 1.0, 1.0, "JPY", scan),   # USD FY value must never be used
        ])
        acols = ("security_id", "fiscal_year", "metric", "value_local", "currency_local", "snapshot_id")
        store.upsert_many(con, "fundamentals_annual", acols, [
            ("NYSE:ROBO", 2022, "total_revenue", 100.0, "USD", scan),
            ("NYSE:ROBO", 2025, "total_revenue", 133.1, "USD", scan),
            ("TSE:6000", 2021, "total_revenue", 1000.0, "JPY", scan),
            ("TSE:6000", 2022, "total_revenue", 1100.0, "JPY", scan),
            ("TSE:6000", 2024, "total_revenue", 1331.0, "JPY", scan),
            ("TSE:6000", 2025, "total_revenue", None, "JPY", scan),         # NULL latest year stays NULL
        ])


class KeywordsUnavailable(RuntimeError):   # matched by behaviour: any keywords failure keeps the old path
    pass


HUMANOID_KW = {"idea_en": "Humanoid robots",
               "keywords": {"en": ["humanoid robot"], "zh": ["人形机器人"], "ja": ["ヒューマノイド"], "ko": ["휴머노이드"]},
               "model": "fake-qwen", "cached": False}


class FakeKeywords:
    """Stands in for jevscreen.keywords.generate (the local model is never loaded in these tests)."""

    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result or HUMANOID_KW, error, []

    def __call__(self, cfg, idea, **kw):
        self.calls.append(idea)
        if self.error is not None:
            raise self.error
        return json.loads(json.dumps(self.result))


class StoreCase(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ)       # restored after the test; the kill switch holds inside it too
        env.start()
        self.addCleanup(env.stop)
        safe_env.apply()
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.cfg = config.Config(home=self.home)
        seed(self.cfg, self.home)
        self.log: list = []
        self.kw = FakeKeywords()

    def tearDown(self):
        self._tmp.cleanup()

    def run_screen(self, idea="humanoid robots", **kw):
        kw.setdefault("jev_factory", make_factory(self.log))
        kw.setdefault("out_dir", self.home / "out")
        kw.setdefault("keywords_fn", self.kw)
        return screen.screen(self.cfg, idea, **kw)

    def query(self, sql, params=()):
        with store.session(self.cfg, read_only=True) as con:
            return con.execute(sql, list(params)).fetchall()


class TestScreenFlow(StoreCase):
    def test_funnel_ranking_and_outputs(self):
        res = self.run_screen(l1_rescue=0)         # GearCo's rescued L1 miss: TestL1Rescue
        self.assertEqual(res["status"], "ok")
        f = res["funnel"]
        # TINY is below 2e8; NODS has no description.
        self.assertEqual((f["universe"], f["described"], f["l1_sent"], f["l1_pass"], f["l2_sent"], f["output"]),
                         (6, 5, 5, 3, 3, 2))
        # every step is counted, from all universe rows
        self.assertEqual((f["universe_rows"], f["null_mcap"], f["below_min_mcap"], f["below_min_volume"],
                          f["other_country"], f["no_description"]), (7, 0, 1, 0, 0, 1))
        self.assertEqual((f["l1_ok"], f["l2_ok"], f["l2_sec_inputs"], f["l2_profile_inputs"], f["l2_verified"],
                          f["l2_contradicted"], f["unverified"]), (5, 3, 2, 1, 2, 0, 1))
        self.assertEqual(res["layers"]["l1"]["labels"], {"adjacent": 2, "core": 2, "unrelated": 1})
        self.assertEqual(res["layers"]["l2"]["labels"], {"explicit": 1, "insufficient": 1, "partial": 1})
        self.assertEqual([g["security_id"] for g in res["gaps"]["no_description"]], ["NYSE:NODS"])
        self.assertEqual([g["security_id"] for g in res["gaps"]["l2_profile_only"]], ["TSE:6000"])
        rows = res["rows"]
        # only L2-verified companies are ranked; 'insufficient' goes to the unverified list
        self.assertEqual([r["security_id"] for r in rows], ["NYSE:ROBO", "NASDAQ:ROB2"])
        self.assertEqual([r["l2_label"] for r in rows], ["explicit", "partial"])
        self.assertEqual([r["l2_evidence"] for r in rows], ["annual_report", "annual_report"])
        robo, rob2 = rows
        self.assertEqual([r["security_id"] for r in res["unverified"]], ["TSE:6000"])
        servo = res["unverified"][0]
        self.assertEqual((servo["rank"], servo["l2_status"], servo["l2_label"], servo["l2_evidence"]),
                         (None, "insufficient", "insufficient", "profile"))
        self.assertEqual((robo["filing_form"], robo["filing_date"], robo["l2_doc_stale"], robo["l2_keyword_hit"]),
                         ("10-K", "2026-02-01", False, True))
        self.assertEqual(robo["evidence_url"], "https://www.sec.gov/robo.htm")
        self.assertEqual(robo["l2_input_tier"], "official-private")
        self.assertIn("humanoid robots", robo["evidence_excerpt"].lower())
        self.assertLessEqual(len(robo["evidence_excerpt"]), 300)
        self.assertEqual(rob2["evidence_url"], "https://www.sec.gov/rob2.htm")   # via sec_cik identifier
        self.assertEqual(servo["l2_input_source"], "profile")
        self.assertEqual(servo["l2_input_tier"], "gray-private")
        self.assertIsNone(servo["evidence_url"])
        self.assertEqual(robo["region"], "US")
        self.assertEqual(servo["region"], "Japan")
        self.assertEqual(robo["l1_request_id"], "l1-req-1")
        self.assertEqual(robo["revenue_ttm_usd"], 1.5e9)
        self.assertAlmostEqual(robo["revenue_cagr_3y_local"], 0.10, places=6)
        # ServoJP: 2025 is NULL, so the latest year is 2024 and the base 2021, in JPY (not the USD FY value)
        self.assertAlmostEqual(servo["revenue_cagr_3y_local"], 0.10, places=6)
        self.assertEqual((servo["cagr_currency"], servo["cagr_years"]), ("JPY", "2021-2024"))
        self.assertIsNone(rob2["revenue_cagr_3y_local"])      # no history -> NULL, not 0
        self.assertIsNone(rob2["revenue_ttm_usd"])

        out = self.home / "out"
        for name in ("results.csv", "results.json", "report.md"):
            self.assertTrue((out / name).exists(), name)
        with open(out / "results.csv", encoding="utf-8") as fh:
            csv_rows = list(csv.DictReader(fh))
        self.assertEqual([r["security_id"] for r in csv_rows], ["NYSE:ROBO", "NASDAQ:ROB2"])
        self.assertEqual(json.loads((out / "results.json").read_text())["run_id"], res["run_id"])
        md = (out / "report.md").read_text()
        for needle in ("## Funnel", "## Ranked results", "## Gaps", "NYSE:NODS", "Personal use only", "cache hits",
                       "## Unverified L1 passes", "NULL market cap", "L1 labels:", "L2 inputs: 2 annual-report",
                       "profile only"):
            self.assertIn(needle, md)

        # persisted
        run = self.query("SELECT status, universe_n, l1_n, l1_pass_n, l2_n, output_n, cost_usd, output_dir "
                         "FROM screen_runs WHERE run_id = ?", [res["run_id"]])
        self.assertEqual(run[0][:6], ("ok", 6, 5, 3, 3, 2))
        self.assertAlmostEqual(run[0][6], 0.02)
        self.assertEqual(run[0][7], str(out))
        counts = dict(self.query("SELECT layer, count(*) FROM screen_results WHERE run_id = ? GROUP BY 1",
                                 [res["run_id"]]))
        self.assertEqual(counts, {"l1": 5, "l2": 3})
        tier = self.query("SELECT input_tier, input_source, evidence_url FROM screen_results "
                          "WHERE run_id = ? AND layer = 'l2' AND security_id = 'NYSE:ROBO'", [res["run_id"]])
        self.assertEqual(tier[0], ("official-private", "sec_filing_text:10-K", "https://www.sec.gov/robo.htm"))

    def test_questions_embed_idea_verbatim(self):
        idea = "人形机器人 humanoid robots"
        self.run_screen(idea=idea, translate=False)
        self.assertEqual(self.kw.calls, [])
        l1, l2 = self.log
        q1, q2 = l1.classified[0][1], l2.classified[0][1]
        self.assertEqual(q1.key, "fit")
        self.assertIn(f'"{idea}"', q1.instructions)
        self.assertEqual(set(q1.criteria), {"core", "adjacent", "unrelated", "insufficient"})
        self.assertEqual(q2.key, "evidence")
        self.assertIn(f'"{idea}"', q2.instructions)
        self.assertEqual(set(q2.criteria), {"explicit", "partial", "contradicted", "insufficient"})
        item = next(i for i in l1.classified[0][0] if i.item_id == "isin:US0000000001")
        self.assertEqual(item.issuer, "RoboCorp")
        self.assertTrue(item.text.startswith("[tradingview_profile] "))

    def test_budget_split(self):
        res = self.run_screen(budget_usd=0.05)
        l1, l2 = self.log
        self.assertEqual(l1.layer, "l1")
        self.assertAlmostEqual(l1.budget_usd, 0.05)
        self.assertEqual(l2.layer, "l2")
        self.assertAlmostEqual(l2.budget_usd, 0.04)       # remaining after one $0.01 L1 request
        self.assertAlmostEqual(res["cost_usd"], 0.02)
        self.assertEqual(res["layers"]["l1"]["requests"], 1)

    def test_budget_exhausted_before_l1_finished(self):
        res = self.run_screen(budget_usd=0.015, jev_factory=make_factory(self.log, pack_size=2))
        self.assertEqual(res["status"], "budget_exhausted")
        self.assertEqual(res["funnel"]["l1_sent"], 2)
        self.assertEqual(len(res["gaps"]["l1_skipped_budget"]), 3)
        self.assertEqual(len(self.log), 1)                # no L2 client
        self.assertTrue((self.home / "out" / "report.md").exists())
        self.assertIn("L1 skipped: budget exhausted", (self.home / "out" / "report.md").read_text())
        run = self.query("SELECT status FROM screen_runs WHERE run_id = ?", [res["run_id"]])
        self.assertEqual(run[0][0], "budget_exhausted")

    def test_l2_max_and_no_budget_for_l2(self):
        res = self.run_screen(l2_max=1, l1_rescue=0)
        self.assertEqual(res["status"], "partial")
        self.assertEqual(res["funnel"]["l2_sent"], 1)
        self.assertEqual(len(res["gaps"]["l2_not_sent_l2_max"]), 2)
        self.assertEqual(res["rows"][0]["security_id"], "NYSE:ROBO")
        self.log.clear()
        res = self.run_screen(budget_usd=0.01, out_dir=self.home / "out2", l1_rescue=0)
        self.assertEqual(res["status"], "partial")
        self.assertEqual(len(self.log), 1)                # L2 client never built: nothing left
        self.assertEqual(len(res["gaps"]["l2_skipped_budget"]), 3)

    def test_filters(self):
        res = self.run_screen(countries=["JP"])
        self.assertEqual(res["funnel"]["universe"], 1)
        self.assertEqual(res["funnel"]["other_country"], 5)
        self.assertEqual(res["rows"], [])                          # insufficient: not ranked
        self.assertEqual([r["security_id"] for r in res["unverified"]], ["TSE:6000"])
        res = self.run_screen(countries=["Europe & UK", "Japan"], out_dir=self.home / "o2")
        self.assertEqual(res["funnel"]["universe"], 2)
        res = self.run_screen(min_avg_volume=1e6, out_dir=self.home / "o3")
        self.assertEqual(res["funnel"]["universe"], 5)    # ServoJP (5e4) out
        self.assertEqual(res["funnel"]["below_min_volume"], 1)
        res = self.run_screen(min_mcap_usd=4.5e9, out_dir=self.home / "o4")
        self.assertEqual(res["funnel"]["universe"], 2)    # ROBO, BANK

    def test_dry_run_writes_nothing(self):
        res = self.run_screen(dry_run=True)
        self.assertEqual(res["status"], "dry_run")
        self.assertTrue(all(not c.classified for c in self.log))
        self.assertTrue(all(c.dry_run for c in self.log))
        self.assertEqual(res["layers"]["l1"]["estimate"]["requests"], 1)
        self.assertEqual(res["layers"]["l2"]["inputs"], 5)
        self.assertIn("est_seconds", res["layers"]["l2"]["estimate"])
        self.assertEqual(res["cost_usd"], 0.0)
        self.assertEqual(self.query("SELECT count(*) FROM screen_runs")[0][0], 0)
        self.assertEqual(self.query("SELECT count(*) FROM screen_results")[0][0], 0)
        md = (self.home / "out" / "report.md").read_text()
        self.assertIn("dry run", md)
        self.assertIn("est. cost", md)

    def test_jev_unavailable(self):
        def factory(cfg, **kw):
            raise JevUnavailable("no key")
        res = self.run_screen(jev_factory=factory)
        self.assertEqual(res["status"], "jev_unavailable")
        self.assertEqual(res["rows"], [])
        self.assertTrue((self.home / "out" / "report.md").exists())

    def test_default_out_dir(self):
        res = self.run_screen(idea="Humanoid Robots!", out_dir=None)
        out = Path(res["output_dir"])
        self.assertEqual(out.parent, self.home / "screens")
        self.assertRegex(out.name, r"^\d{8}-\d{4}-humanoid-robots$")
        res2 = self.run_screen(idea="人形机器人", out_dir=None)
        self.assertRegex(Path(res2["output_dir"]).name, r"^\d{8}-\d{4}-idea-[0-9a-f]{8}$")


class FailingL2Jev(FakeJev):
    """L2 requests fail for every item (L1 as usual)."""

    def classify(self, items, question):
        if question.key != "evidence":
            return super().classify(items, question)
        self._spent += self.COST
        self._sent += 1
        return [{"item_id": it.item_id, "label": None, "probs": {}, "request_id": "l2-req-1", "status": "failed",
                 "error": "invalid_response:model_mismatch", "cached": False} for it in items]


class UnavailableMidL1Jev(FakeJev):
    """Answers the first pack, then the provider refuses (402): JevUnavailable carries the completed results."""

    def classify(self, items, question):
        res = super().classify(items[:2], question)
        e = JevUnavailable("HTTP 402")
        e.results = res + [{"item_id": it.item_id, "label": None, "probs": {}, "request_id": None,
                            "status": "failed", "error": "not sent: provider_unavailable", "cached": False}
                           for it in items[2:]]
        raise e


class TestReviewFixes(StoreCase):
    def test_all_contradicted_outputs_nothing(self):
        contra = lambda text: ("contradicted", {"explicit": 0.05, "partial": 0.05, "contradicted": 0.85,  # noqa
                                                "insufficient": 0.05})
        with mock.patch.object(sys.modules[__name__], "l2_label", contra):
            res = self.run_screen()
        self.assertEqual(res["status"], "ok")
        self.assertEqual((res["rows"], res["unverified"]), ([], []))
        self.assertEqual(res["funnel"]["l2_contradicted"], 3)
        self.assertEqual(len(res["gaps"]["l2_contradicted"]), 3)
        md = (self.home / "out" / "report.md").read_text()
        self.assertIn("no company passed layer 2", md)
        self.assertIn("L2 contradicted (dropped from the output)", md)

    def test_failed_l2_is_unverified_without_evidence(self):
        log = []
        factory = lambda cfg, **kw: FailingL2Jev(cfg, log=log, **kw)  # noqa: E731
        res = self.run_screen(jev_factory=factory)
        self.assertEqual(res["status"], "partial")
        self.assertEqual(res["rows"], [])
        unv = {r["security_id"]: r for r in res["unverified"]}
        self.assertEqual(set(unv), {"NYSE:ROBO", "NASDAQ:ROB2", "TSE:6000"})
        robo = unv["NYSE:ROBO"]
        self.assertEqual(robo["l2_status"], "failed")
        for k in ("l2_label", "evidence_excerpt", "evidence_url", "l2_input_tier", "l2_input_source", "l2_evidence",
                  "filing_date"):
            self.assertIsNone(robo[k], k)                   # never shown as evidence: Jev did not judge it
        self.assertEqual(res["tiers_used"], ["gray-private", "official-private"])   # L1 tiers only

    def test_unavailable_mid_l1_keeps_paid_answers(self):
        log = []
        factory = lambda cfg, **kw: UnavailableMidL1Jev(cfg, log=log, **kw)  # noqa: E731
        res = self.run_screen(jev_factory=factory)
        self.assertEqual(res["status"], "jev_unavailable")
        self.assertEqual(res["funnel"]["l1_ok"], 2)
        self.assertEqual(res["layers"]["l1"]["by_status"], {"failed": 3, "ok": 2})
        self.assertAlmostEqual(res["cost_usd"], 0.01)
        self.assertTrue(any("L1 stopped after 1 requests" in n for n in res["notes"]))
        n = self.query("SELECT count(*) FROM screen_results WHERE run_id = ? AND layer = 'l1' AND status = 'ok'",
                       [res["run_id"]])[0][0]
        self.assertEqual(n, 2)

    def test_jev_busy(self):
        class JevBusy(JevUnavailable):
            pass

        def factory(cfg, **kw):
            raise JevBusy("another process")
        res = self.run_screen(jev_factory=factory)
        self.assertEqual(res["status"], "jev_busy")

    def test_fundamentals_lock_still_writes_outputs(self):
        real = store.session
        calls = {"n": 0}

        @contextlib.contextmanager
        def flaky(cfg, *, read_only=False, **kw):
            if read_only:
                calls["n"] += 1
                if calls["n"] == 2:                       # 1 = universe, 2 = fundamentals
                    raise store.StoreLocked("held by sync-sec")
            with real(cfg, read_only=read_only, **kw) as con:
                yield con

        with mock.patch.object(store, "session", flaky):
            res = self.run_screen()
        self.assertEqual(res["status"], "partial")
        self.assertIsNone(res["rows"][0]["revenue_ttm_usd"])
        self.assertTrue(any("fundamentals: store locked" in n for n in res["notes"]))
        self.assertTrue((self.home / "out" / "report.md").exists())
        self.assertEqual(self.query("SELECT status FROM screen_runs WHERE run_id = ?", [res["run_id"]])[0][0],
                         "partial")

    def test_interrupt_marks_run(self):
        class Interrupting(FakeJev):
            def classify(self, items, question):
                self._spent += 0.03
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.run_screen(jev_factory=lambda cfg, **kw: Interrupting(cfg, **kw))
        row = self.query("SELECT status, cost_usd, finished_at FROM screen_runs")[0]
        self.assertEqual(row[:2], ("interrupted", 0.03))
        self.assertIsNotNone(row[2])

    def test_unknown_country_is_an_error(self):
        with self.assertRaises(ValueError):
            self.run_screen(countries=["Jpan"])
        self.assertEqual(self.query("SELECT count(*) FROM screen_runs")[0][0], 0)
        res = self.run_screen(countries=["japan"])              # store country names are accepted
        self.assertEqual(res["funnel"]["universe"], 1)

    def test_bad_parameters(self):
        for kw in ({"budget_usd": float("nan")}, {"budget_usd": -1}, {"min_mcap_usd": float("nan")},
                   {"max_out": 0}, {"l2_max": -1}, {"min_avg_volume": float("inf")}):
            with self.assertRaises(ValueError, msg=kw):
                self.run_screen(**kw)

    def test_retry_uncertain_reaches_the_client(self):
        seen = []

        def factory(cfg, **kw):
            seen.append(kw.get("retry_uncertain"))
            kw.pop("retry_uncertain", None)
            return FakeJev(cfg, **kw)
        self.run_screen(jev_factory=factory, retry_uncertain=True)
        self.assertEqual(seen, [True, True])
        seen.clear()
        self.run_screen(jev_factory=factory, out_dir=self.home / "o2")
        self.assertEqual(seen, [None, None])                     # old factories keep working

    def test_l2_text_is_tagged_and_old_filings_flagged(self):
        with store.session(self.cfg) as con:
            con.execute("UPDATE documents SET filing_date = DATE '2012-03-26' WHERE cik = '777'")
        res = self.run_screen()
        l2 = self.log[1].classified[0][0]
        texts = {it.item_id: it.text for it in l2}
        self.assertTrue(texts["isin:US0000000001"].startswith(
            "[annual report excerpts: SEC 10-K filed 2026-02-01; language en]"))
        self.assertTrue(texts["isin:JP0000000002"].startswith("[company profile; no annual report text"))
        rob2 = next(r for r in res["rows"] if r["security_id"] == "NASDAQ:ROB2")
        self.assertEqual((rob2["filing_date"], rob2["l2_doc_stale"]), ("2012-03-26", True))
        self.assertEqual([g["security_id"] for g in res["gaps"]["sec_document_old"]], ["NASDAQ:ROB2"])
        q2 = self.log[1].classified[0][1]
        self.assertIn("company profile", q2.instructions)
        self.assertNotIn("unrelated", q2.criteria["contradicted"])
        self.assertIn("other businesses", q2.criteria["insufficient"])

    def test_profile_evidence_weighs_less(self):
        s = screen.score_of
        self.assertGreater(s("explicit", 0.9, 1e9, "annual_report"), s("explicit", 0.9, 1e9, "profile"))
        self.assertEqual(s("insufficient", 0.9, 1e9, "profile"), s("insufficient", 0.9, 1e9, "annual_report"))

    def test_keyword_warning_for_non_english_idea(self):
        res = self.run_screen(idea="企业 AI agent 的身份与权限管控", translate=False)
        self.assertEqual(res["terms"], ["AI agent", "AI", "agent"])
        self.assertTrue(res["warnings"] and "--keywords" in res["warnings"][0])
        self.assertIn("## Warnings", (self.home / "out" / "report.md").read_text())
        res = self.run_screen(idea="企业 AI agent 的身份与权限管控", keywords=["AI agent", "identity"],
                              out_dir=self.home / "o2")
        self.assertEqual(res["warnings"], [])

    def test_dry_run_budget_and_split(self):
        res = self.run_screen(dry_run=True)
        b = res["dry_run_budget"]
        self.assertEqual(set(b), {"est_cost_usd", "est_reserved_usd", "budget_usd", "reservation_exceeds_budget",
                                  "est_seconds"})
        self.assertEqual((res["funnel"]["l2_sec_inputs"], res["funnel"]["l2_profile_inputs"]), (2, 3))
        md = (self.home / "out" / "report.md").read_text()
        for needle in ("Estimate against the budget", "reserved against the budget", "upper bound",
                       "No annual report text"):
            self.assertIn(needle, md)
        res = self.run_screen(dry_run=True, budget_usd=0.001, out_dir=self.home / "o2")
        self.assertTrue(res["dry_run_budget"]["reservation_exceeds_budget"])
        self.assertTrue(any("exceeds the budget" in w for w in res["warnings"]))


class TestPureHelpers(unittest.TestCase):
    def test_description_priority(self):
        d = screen.select_description([
            ("sec_filing_text", "SEC text about the business.", True),
            ("financedatabase_local", "Yahoo summary text.", True),
            ("tradingview_profile", "TV profile text.", True),
        ])
        self.assertEqual(d["sources"], ["tradingview_profile", "financedatabase_local"])
        self.assertTrue(d["text"].startswith("[tradingview_profile] TV profile text."))
        self.assertIn("[financedatabase_local] Yahoo summary text.", d["text"])
        self.assertEqual(d["tier"], "gray-private")
        # duplicates are not "distinct": the next source is used instead
        d = screen.select_description([("tradingview_profile", "Same text.", True),
                                       ("financedatabase_local", "same   TEXT.", True),
                                       ("sec_filing_text", "Other.", False)])
        self.assertEqual(d["sources"], ["tradingview_profile", "sec_filing_text"])
        # SEC only -> official-private; own line wins over a longer text on another line
        d = screen.select_description([("sec_filing_text", "own line", True),
                                       ("sec_filing_text", "a much longer text on another line", False)])
        self.assertEqual((d["sources"], d["tier"], d["text"]), (["sec_filing_text"], "official-private",
                                                                "[sec_filing_text] own line"))
        self.assertIsNone(screen.select_description([("tradingview_profile", "  ", True)]))
        long = screen.select_description([("tradingview_profile", "x " * 1500, True),
                                          ("financedatabase_local", "y " * 1500, True)])
        self.assertLessEqual(len(long["text"]), screen.DESC_MAX_CHARS)
        self.assertIn("[financedatabase_local]", long["text"])

    def test_l1_thresholds(self):
        ok = lambda label, probs: {"status": "ok", "label": label, "probs": probs}  # noqa: E731
        self.assertTrue(screen.l1_passes(ok("core", {"core": 0.5})))
        self.assertTrue(screen.l1_passes(ok("adjacent", {"core": 0.3, "adjacent": 0.3})))
        self.assertFalse(screen.l1_passes(ok("adjacent", {"core": 0.2, "adjacent": 0.3})))
        self.assertFalse(screen.l1_passes(ok("adjacent", {"core": 0.3, "adjacent": 0.3}), adjacent_min=0.7))
        self.assertFalse(screen.l1_passes(ok("core", {"core": 0.5}), core_min=0.6))
        self.assertFalse(screen.l1_passes(ok("unrelated", {"core": 0.4, "adjacent": 0.4})))
        self.assertFalse(screen.l1_passes({"status": "failed", "label": "core", "probs": {}}))
        self.assertFalse(screen.l1_passes(None))

    def test_excerpts(self):
        ex = screen.build_excerpts(SEC_TEXT_ROBO, terms=["Humanoid"],
                                   description="RoboCorp competes with large industrial conglomerates on price")
        self.assertEqual([e["kind"] for e in ex], ["overview", "keywords", "description"])
        self.assertIn("RoboCorp designs and manufactures", ex[0]["text"])
        self.assertIn("humanoid robots", ex[1]["text"].lower())
        self.assertIn("conglomerates", ex[2]["text"])
        self.assertTrue(all(len(e["text"]) <= screen.EXCERPT_MAX_CHARS for e in ex))
        # no terms, no description -> overview only; deterministic
        self.assertEqual(screen.build_excerpts(SEC_TEXT_ROBO), screen.build_excerpts(SEC_TEXT_ROBO))
        self.assertEqual(len(screen.build_excerpts(SEC_TEXT_ROBO)), 1)
        # long paragraph: every excerpt is capped
        big = "Intro paragraph " * 100 + "\n\n" + ("filler words " * 80 + "humanoid robots here " + "tail " * 200)
        for e in screen.build_excerpts(big, terms=["humanoid"]):
            self.assertLessEqual(len(e["text"]), screen.EXCERPT_MAX_CHARS)
        kw = screen.build_excerpts(big, terms=["humanoid"])[1]["text"]
        self.assertIn("humanoid", kw)
        # whole-word match for ASCII terms: 'AI' must not match 'maintain'
        text = ("Overview paragraph that is long enough to be kept as a paragraph by the splitter.\n\n"
                "We maintain our plants carefully and this paragraph is also long enough to count.\n\n"
                "Our AI platform powers inference for customers and this paragraph is long enough too.\n\n"
                "Closing paragraph about employees that is long enough to be kept by the splitter.")
        self.assertIn("AI platform", screen.build_excerpts(text, terms=["ai"])[1]["text"])

    def test_idea_terms(self):
        self.assertEqual(screen.idea_terms("Humanoid robots and the robot supply chain", None),
                         ["Humanoid robots", "robot supply chain", "Humanoid", "robots", "robot", "supply", "chain"])
        self.assertEqual(screen.idea_terms("人形机器人", None), [])
        self.assertEqual(screen.idea_terms("人形机器人", [" servo ", "", "reducer"]), ["servo", "reducer"])

    def test_terms_acronyms_phrases_inflections(self):
        self.assertEqual(screen.idea_terms("AI chips for EV", None), ["AI chips", "AI", "chips", "EV"])
        self.assertEqual(screen.idea_terms("5G base stations", None), ["5G base stations", "5G", "base", "stations"])
        agent = screen._term_pattern("agent")
        for w in ("agent", "agents", "Agentic", "AI agents."):
            self.assertTrue(agent.search(w), w)
        self.assertFalse(agent.search("reagent"))
        ai = screen._term_pattern("AI")
        self.assertTrue(ai.search("our AI platform") and ai.search("AIs"))
        self.assertFalse(ai.search("said ai") or ai.search("maintain"))
        phrase = screen._term_pattern("AI agent")
        self.assertTrue(phrase.search("governs AI  agents acting"))
        self.assertFalse(phrase.search("paying agent"))
        self.assertTrue(screen._term_pattern("access management").search("privileged Access Management"))

    def test_country_matcher_rejects_unknown(self):
        with self.assertRaises(ValueError):
            screen.country_matcher(["Jpan"])
        self.assertTrue(screen.country_matcher(["Vanuatu"], {"Vanuatu"})("Vanuatu", "XX"))
        m = screen.country_matcher(["Hong Kong"])
        self.assertTrue(m("Cayman Islands", "HKEX"))          # offshore-incorporated, HK-listed

    def test_cagr_local(self):
        g = screen.revenue_cagr_local([(2022, 100.0, "EUR"), (2025, 133.1, "EUR"), (2024, 120.0, "EUR")])
        self.assertAlmostEqual(g[0], 0.1)
        self.assertEqual(g[1:], ("EUR", "2022-2025"))
        self.assertIsNone(screen.revenue_cagr_local([(2022, 100.0, "GBP"), (2025, 133.1, "USD")]))   # never mix
        self.assertIsNone(screen.revenue_cagr_local([(2022, None, "EUR"), (2025, 133.1, "EUR")]))
        self.assertIsNone(screen.revenue_cagr_local([(2022, -5.0, "EUR"), (2025, 133.1, "EUR")]))
        self.assertIsNone(screen.revenue_cagr_local([]))

    def test_score_order(self):
        s = screen.score_of
        self.assertGreater(s("explicit", 0.5, 1e9), s("partial", 0.9, 1e12))
        self.assertGreater(s("partial", 0.9, 1e9), s(None, 0.9, 1e9))
        self.assertGreater(s("insufficient", 0.9, 1e9), s(None, 0.9, 1e9))
        self.assertLess(s("contradicted", 0.9, 1e9), s(None, 0.9, 1e9))
        self.assertGreater(s("partial", 0.9, 1e10), s("partial", 0.9, 1e9))    # market cap only breaks ties
        self.assertLess(s("partial", 0.9, 1e12) - s("partial", 0.9, 1e9), 0.1)

    def test_country_matcher(self):
        m = screen.country_matcher(["US", "jp", "Europe & UK"])
        self.assertTrue(m("United States", "NYSE"))
        self.assertTrue(m("Cayman Islands", "NASDAQ"))     # region by listing venue
        self.assertTrue(m("Japan", "TSE"))
        self.assertTrue(m("Germany", "XETR"))
        self.assertFalse(m("China", "SSE"))
        self.assertTrue(screen.country_matcher(None)(None, None))

    def test_estimate_seconds(self):
        self.assertEqual(screen.estimate_seconds(0), 0.0)
        self.assertAlmostEqual(screen.estimate_seconds(32, rate=15, workers=16, latency=4), 8.0)
        self.assertAlmostEqual(screen.estimate_seconds(300, rate=15, workers=16, latency=0.1), 20.0)


class TestCli(StoreCase):
    def main(self, argv, factory=None):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"JEVSCREEN_HOME": str(self.home)}), \
                mock.patch.object(screen, "_default_factory", factory or make_factory(self.log)), \
                mock.patch.object(screen, "_default_keywords", self.kw), \
                contextlib.redirect_stdout(buf):
            code = cli.main(argv)
        return code, buf.getvalue()

    def test_parse(self):
        a = cli.build_parser().parse_args(["screen", "人形机器人", "--min-mcap", "5e8", "--min-volume", "1000",
                                           "--countries", "US, JP", "--max-out", "10", "--budget", "1.5",
                                           "--l2-max", "50", "--keywords", "humanoid,servo", "--dry-run"])
        self.assertEqual((a.idea, a.min_mcap, a.min_volume, a.countries, a.max_out, a.budget, a.l2_max, a.keywords,
                          a.dry_run), ("人形机器人", 5e8, 1000.0, ["US", "JP"], 10, 1.5, 50, ["humanoid", "servo"],
                                       True))
        d = cli.build_parser().parse_args(["screen", "x"])
        self.assertEqual((d.min_mcap, d.min_volume, d.countries, d.max_out, d.budget, d.l2_max, d.keywords,
                          d.dry_run), (2e8, None, None, 40, 3.0, 600, None, False))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["screen", "x", "--countries", " , "])

    def test_ok_exit_and_output(self):
        code, out = self.main(["screen", "humanoid robots"])
        self.assertEqual(code, 0)
        self.assertIn("status=ok", out)
        self.assertIn("universe 6", out)
        self.assertIn("NYSE:ROBO", out)
        self.assertIn(str(self.home / "screens"), out)
        self.assertNotIn("sk-", out)

    def test_budget_exit(self):
        code, out = self.main(["screen", "humanoid robots", "--budget", "0.015"],
                              factory=make_factory(self.log, pack_size=2))
        self.assertEqual(code, 5)
        self.assertIn("budget_exhausted", out)
        self.assertEqual(len(list((self.home / "screens").glob("*/report.md"))), 1)

    def test_unavailable_exit(self):
        def factory(cfg, **kw):
            raise JevUnavailable("401")
        code, _ = self.main(["screen", "humanoid robots"], factory=factory)
        self.assertEqual(code, 6)

    def test_parse_translation_flags(self):
        a = cli.build_parser().parse_args(["screen", "人形机器人", "--no-translate", "--keywords-zh", "人形机器人，减速器",
                                           "--keywords-ja", "ヒューマノイド", "--keywords-ko", "휴머노이드"])
        self.assertEqual((a.no_translate, a.keywords_zh, a.keywords_ja, a.keywords_ko),
                         (True, ["人形机器人", "减速器"], ["ヒューマノイド"], ["휴머노이드"]))
        d = cli.build_parser().parse_args(["screen", "x"])
        self.assertEqual((d.no_translate, d.keywords_zh, d.keywords_ja, d.keywords_ko), (False, None, None, None))

    def test_cli_translates_and_no_translate(self):
        code, out = self.main(["screen", "人形机器人", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(self.kw.calls, ["人形机器人"])
        self.assertIn("idea (en): Humanoid robots", out)
        code, out = self.main(["screen", "人形机器人", "--dry-run", "--no-translate"])
        self.assertEqual((code, self.kw.calls), (0, ["人形机器人"]))
        self.assertIn("WARNING", out)

    def test_dry_run_exit(self):
        code, out = self.main(["screen", "humanoid robots", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("L1 estimate: 1 requests", out)
        self.assertEqual(self.query("SELECT count(*) FROM screen_runs")[0][0], 0)


class TestCliValidation(StoreCase):
    def test_numbers_are_checked(self):
        for argv in (["--budget", "nan"], ["--budget", "-1"], ["--max-out", "0"], ["--l2-max", "-1"],
                     ["--min-mcap", "nan"], ["--min-volume", "-5"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit, msg=argv):
                cli.build_parser().parse_args(["screen", "x"] + argv)
        a = cli.build_parser().parse_args(["screen", "x", "--retry-uncertain"])
        self.assertTrue(a.retry_uncertain)

    def test_unknown_country_and_busy_exit_codes(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"JEVSCREEN_HOME": str(self.home)}), contextlib.redirect_stderr(err), \
                mock.patch.object(screen, "_default_keywords", self.kw), contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["screen", "x", "--countries", "Jpan", "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("unknown country", err.getvalue())

        class JevBusy(JevUnavailable):
            pass

        def factory(cfg, **kw):
            raise JevBusy("busy")
        with mock.patch.dict(os.environ, {"JEVSCREEN_HOME": str(self.home)}), \
                mock.patch.object(screen, "_default_factory", factory), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["screen", "humanoid robots"]), 4)
        self.assertEqual(cli.SCREEN_EXIT["budget_exhausted"], 5)
        self.assertEqual(cli.SCREEN_EXIT["jev_unavailable"], 6)

ZH_FULL = """第三节 管理层讨论与分析

一、报告期内公司从事的主要业务

公司是国内领先的工业自动化设备供应商，主要产品包括伺服系统、运动控制器和工业机器人整机，产品广泛应用于汽车、消费电子、锂电池和仓储物流等行业，客户覆盖国内外主要制造企业。

公司研发并销售人形机器人，主要用于仓储拣选和装配作业。报告期内人形机器人业务收入同比大幅增长，已在多家客户现场实现批量部署。

公司拥有完整的研发体系和专利布局，在运动控制算法、精密减速器和多传感器融合方面具有核心技术优势，研发人员占员工总数的比例超过三成。
"""

ZH_SUMMARY = """一、重要提示

本摘要只列示年度报告的要点，完整的经营数据、财务报表以及下一年度的发展计划，请以同日发布在公司网站上的年度报告全文为准。公司主要从事工业自动化设备的研发、生产和销售。
"""

JA_YUHO = """3【事業の内容】

当社グループは、工場自動化向けのサーボモーターおよび精密減速機の製造・販売を主な事業としております。主要な顧客は自動車メーカーおよび電子部品メーカーであり、国内外に販売拠点を有しております。

当社は、ヒューマノイドロボット向けの小型アクチュエーターの開発を進めており、一部の顧客に試作品を出荷しております。

当社グループの従業員数は連結で約1,200名であり、そのうち研究開発に従事する者は約300名であります。
"""

KO_DART = """II. 사업의 내용

당사는 산업용 로봇과 협동 로봇을 설계하고 제조하는 기업으로, 자동차 및 전자 산업의 고객에게 제품을 공급하고 있습니다. 주요 생산 시설은 국내에 위치하고 있습니다.

당사는 물류 창고용 휴머노이드 로봇을 개발하여 판매하고 있으며, 휴머노이드 로봇 매출은 전년 대비 크게 증가하였습니다.

당사의 임직원 수는 약 800명이며, 연구개발 인력이 전체 인원의 40% 이상을 차지하고 있습니다.
"""


# An EDINET 有価証券報告書 as stored: the XBRL text blocks arrive flattened, one line per block, the old paragraph
# breaks surviving only as single spaces. SYNTHETIC text written from scratch for an invented issuer ("Sorano"): only
# the statutory headings, the i. / ii. item markers and the standard 将来に関する事項 disclaimer follow the yuho form.
JA_YUHO_FLAT = (
    "３ 【事業の内容】 当社グループは、当社及び子会社１社（Sorano Systems Pte. Ltd.）で構成され、「働く場所を選ばない"
    "安心を、すべての組織に」を使命として、中小規模の組織向けに情報セキュリティの仕組みを定額制のクラウドサービスとして"
    "開発・提供しております。 "
    "近年、在宅勤務や外出先からの業務が一般化し、社員が自宅や移動中に会社の情報へ接続する機会が大きく増えました。一方で、"
    "専任の情報システム担当者を置けない組織では、パスワードの使い回しや共有端末の放置、取引先を装った不審なメールへの対応が"
    "後回しになりがちであり、クラウドの利点を十分に生かせていないのが実情です。さらに、取引先から情報管理体制の証明を"
    "求められる場面も増えており、限られた人員で運用できる対策への需要が高まっております。当社はこうした組織に向けて、"
    "導入初日から使える初期設定と、管理画面の日本語サポートを重視したサービス設計を行っております。 "
    "当社の主力サービス「Sorano Suite」は、利用者の本人確認と接続管理を担う領域と、メールと添付ファイルの取り扱いを守る"
    "領域の２つから成り立っております。 "
    "i. Sorano Pass 社員が使う各種クラウドサービスへの入口をひとつにまとめる機能です。管理者は、接続元の場所や端末の"
    "状態に応じてアクセス制御の条件を細かく設定できます。ワンタイムコードや生体情報を用いた多要素認証（注１）、退職時に"
    "すべてのサービスの利用権限をまとめて停止する一括停止機能（注２）、社外の取引先に期限付きで閲覧権限を与えるゲスト招待"
    "機能などを備え、「Sorano Bridge」を併用すれば社内に残る旧来の業務システムにも同じ入口から接続できます。 "
    "ii. Sorano Mail 送信前の宛先確認や添付ファイルの自動暗号化によって、メール経由の情報流出を防ぐサービスです。"
    "\n\n【経営方針、経営環境及び対処すべき課題等】\n"
    "１ 【経営方針、経営環境及び対処すべき課題等】文中の将来に関する事項は、本書提出日現在において当社グループが判断したもの"
    "であります。（１）経営方針 当社グループは、使命に掲げる安心を、専門知識のない利用者にも手間なく届けることを経営の"
    "基本方針としております。サイバー攻撃は年々巧妙になり、対策製品の種類も増え続けています。その結果、どの製品をどう"
    "組み合わせればよいか判断できない組織が少なくありません。当社は、選ぶ手間と設定の手間を減らすことこそが価値であると"
    "考え、機能の追加よりも使いやすさの改善を優先して開発を進めております。"
)
JA_IAM_TERMS = ["AIエージェント", "エージェント", "ID管理", "アイデンティティ", "認証", "アクセス管理", "アクセス制御", "特権",
                "権限", "ゼロトラスト", "APIセキュリティ"]


def seed_official(cfg: config.Config, home: Path) -> None:
    """Adds a Chinese (CNINFO summary + full), a Korean (DART, attached only through dart_corp_code) company and an
    EDINET document for ServoJP (attached by company_key) to the base seed."""
    docs = home / "docs"
    for name, text in (("zh_full.txt", ZH_FULL), ("zh_summary.txt", ZH_SUMMARY), ("zh_old.txt", ZH_FULL),
                       ("ja.txt", JA_YUHO), ("ko.txt", KO_DART)):
        (docs / name).write_text(text, encoding="utf-8")
    with store.session(cfg) as con:
        snap = lambda src: store.record_snapshot(  # noqa: E731
            con, source_id=src, kind="t", request=None, raw_path=None, raw_sha256=None, raw_bytes=None, rows=None,
            duration_s=None)
        scan, prof = snap("tradingview_scanner"), snap("tradingview_profile")
        cn, ja, ko = snap("cninfo_annual_report"), snap("edinet_yuho"), snap("dart_business_report")
        store.upsert_many(con, "securities", SEC_COLS, [
            ("SZSE:300999", "SZSE", "300999", "China Robo", "CNE000000009", "China", "stock", "common", True,
             "isin:CNE000000009", scan, True),
            ("KRX:090000", "KRX", "090000", "KoRobo", "KR7090000008", "South Korea", "stock", "common", True,
             "isin:KR7090000008", scan, True)])
        store.upsert_many(con, "market_daily", ("security_id", "as_of", "market_cap_usd", "avg_volume_10d",
                                                "snapshot_id"),
                          [("SZSE:300999", D, 6e9, 1e6, scan), ("KRX:090000", D, 7e9, 1e6, scan)])
        store.upsert_many(con, "descriptions", ("security_id", "source_id", "company_key", "text", "snapshot_id"), [
            ("SZSE:300999", "tradingview_profile", "isin:CNE000000009", "China Robo makes industrial robot systems.",
             prof),
            ("KRX:090000", "tradingview_profile", "isin:KR7090000008", "KoRobo builds collaborative robot arms.",
             prof)])
        store.upsert_many(con, "identifiers", ("security_id", "id_type", "id_value", "method", "snapshot_id"), [
            ("KRX:090000", "dart_corp_code", "00999999", "test", ko),
            ("SZSE:300999", "cninfo_orgid", "9900000999", "test", cn)])
        cols = ("doc_id", "security_id", "company_key", "source_id", "cik", "form", "section", "filing_date",
                "report_date", "url", "text_path", "text_chars", "extract_note", "snapshot_id")
        store.upsert_many(con, "documents", cols, [
            ("cninfo_annual_report:9900000999:s25:business", "SZSE:300999", "isin:CNE000000009",
             "cninfo_annual_report", "9900000999", "年度报告摘要", "business", dt.date(2026, 4, 20),
             dt.date(2025, 12, 31), "http://www.cninfo.com.cn/s25.pdf", str(docs / "zh_summary.txt"), 120,
             "kind=summary", cn),
            ("cninfo_annual_report:9900000999:f25:business", "SZSE:300999", "isin:CNE000000009",
             "cninfo_annual_report", "9900000999", "年度报告", "business", dt.date(2026, 4, 19),
             dt.date(2025, 12, 31), "http://www.cninfo.com.cn/f25.pdf", str(docs / "zh_full.txt"), 400, None, cn),
            ("cninfo_annual_report:9900000999:f24:business", "SZSE:300999", "isin:CNE000000009",
             "cninfo_annual_report", "9900000999", "年度报告", "business", dt.date(2025, 4, 18),
             dt.date(2024, 12, 31), "http://www.cninfo.com.cn/f24.pdf", str(docs / "zh_old.txt"), 400, None, cn),
            ("edinet_yuho:E99999:S100TEST:business", None, "isin:JP0000000002", "edinet_yuho", "E99999",
             "有価証券報告書", "business", dt.date(2026, 6, 25), dt.date(2026, 3, 31),
             "https://disclosure2.edinet-fsa.go.jp/S100TEST", str(docs / "ja.txt"), 300, None, ja),
            ("dart_business_report:00999999:20260310000999:business", None, None, "dart_business_report",
             "00999999", "사업보고서", "business", dt.date(2026, 3, 10), dt.date(2025, 12, 31),
             "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260310000999", str(docs / "ko.txt"), 300, None, ko),
        ])


class TestOfficialSources(StoreCase):
    def setUp(self):
        super().setUp()
        seed_official(self.cfg, self.home)

    def test_loader_picks_documents_from_each_source(self):
        with store.session(self.cfg, read_only=True) as con:
            docs = screen.load_documents(con, min_mcap_usd=2e8)
            self.assertEqual(screen.load_sec_documents(con, min_mcap_usd=2e8).keys(), docs.keys())   # old name
        self.assertEqual(docs["isin:US0000000001"]["source_id"], "sec_filing_text")
        self.assertEqual(docs["isin:US0000000007"]["source_id"], "sec_filing_text")        # via sec_cik
        self.assertEqual(docs["isin:JP0000000002"]["source_id"], "edinet_yuho")            # via company_key
        self.assertEqual(docs["isin:KR7090000008"]["source_id"], "dart_business_report")   # via dart_corp_code
        cn = docs["isin:CNE000000009"]
        # same fiscal year: the full report beats the (later-filed) summary; the FY2024 report is older
        self.assertEqual((cn["doc_id"], cn["candidates"]), ("cninfo_annual_report:9900000999:f25:business", 3))

    def test_choose_document_rules(self):
        full = {"doc_id": "f", "form": "年度报告", "report_date": dt.date(2025, 12, 31),
                "filing_date": dt.date(2026, 4, 1), "text_path": "/full.txt"}
        summ = {"doc_id": "s", "form": "年度报告摘要", "report_date": dt.date(2025, 12, 31),
                "filing_date": dt.date(2026, 4, 2), "text_path": "/summary.txt"}
        newer_summ = {**summ, "doc_id": "s2", "report_date": dt.date(2026, 12, 31), "filing_date": dt.date(2027, 4, 2)}
        yes, no = (lambda p: True), (lambda p: False)
        self.assertEqual(screen.choose_document([summ, full], yes)["doc_id"], "f")
        self.assertEqual(screen.choose_document([summ, full], no)["doc_id"], "s")          # full text missing
        self.assertEqual(screen.choose_document([full, newer_summ], yes)["doc_id"], "s2")  # newer year wins
        self.assertIsNone(screen.choose_document([]))
        self.assertTrue(screen.is_summary_doc({"form": "annual_report_summary"}))
        self.assertTrue(screen.is_summary_doc({"form": "年度报告摘要"}))
        self.assertFalse(screen.is_summary_doc({"form": "10-K"}))
        # CNINFO full-report fallback rows carry 'summary' in extract_note only: they are full reports
        for note in ("ok:end=core_competence;summary_failed:start_heading_not_found",
                     "ok:end=core_competence;summary_previously_failed", "ok:end=accounting_data;rules=summary"):
            self.assertFalse(screen.is_summary_doc({"form": "annual_report", "extract_note": note,
                                                    "extractor": "cninfo-v2/pymupdf"}), note)
        fallback = {**full, "form": "annual_report", "extract_note": "ok;summary_failed:no_section"}
        self.assertEqual(screen.choose_document([summ, fallback], yes)["doc_id"], "f")
        # the same-year summary is the full report's companion (read first by layer 2)
        self.assertEqual(screen.summary_companion(full, [summ, full], yes)["doc_id"], "s")
        self.assertIsNone(screen.summary_companion(full, [summ, full], no))
        self.assertIsNone(screen.summary_companion(summ, [summ, full], yes))
        self.assertIsNone(screen.summary_companion(full, [full, newer_summ], yes))
        self.assertEqual(screen.fiscal_year_of({"filing_date": "2026-06-25"}), 2026)

    def test_screen_reads_cjk_documents_with_language_keywords(self):
        res = self.run_screen(idea="人形机器人", l1_rescue=0)
        self.assertEqual(self.kw.calls, ["人形机器人"])
        l2 = {it.item_id: it.text for it in self.log[1].classified[0][0]}
        cn, ja, ko = l2["isin:CNE000000009"], l2["isin:JP0000000002"], l2["isin:KR7090000008"]
        self.assertTrue(cn.startswith("[annual report excerpts: CNINFO 年度报告 + summary filed 2026-04-19; "
                                      "language zh]"), cn[:80])
        self.assertIn("公司主要从事工业自动化设备的研发", cn)       # the same-year summary is read first
        self.assertTrue(ja.startswith("[annual report excerpts: EDINET 有価証券報告書 filed 2026-06-25; language ja]"))
        self.assertTrue(ko.startswith("[annual report excerpts: DART 사업보고서 filed 2026-03-10; language ko]"))
        self.assertIn("人形机器人业务收入", cn)
        self.assertIn("ヒューマノイドロボット", ja)
        self.assertIn("휴머노이드 로봇", ko)
        rows = {r["security_id"]: r for r in res["rows"]}
        for sid, src, lang in (("SZSE:300999", "cninfo_annual_report", "zh"), ("TSE:6000", "edinet_yuho", "ja"),
                               ("KRX:090000", "dart_business_report", "ko")):
            r = rows[sid]
            self.assertEqual((r["filing_source"], r["doc_lang"], r["l2_evidence"], r["l2_keyword_hit"],
                              r["l2_input_tier"], r["l2_label"]),
                             (src, lang, "annual_report", True, "official-private", "explicit"), sid)
            self.assertTrue(r["l2_input_source"].startswith(src + ":"))
        l2s = res["layers"]["l2"]
        self.assertEqual(l2s["inputs_by_source"], {"cninfo_annual_report": 1, "dart_business_report": 1,
                                                   "edinet_yuho": 1, "sec_filing_text": 2})
        self.assertEqual(l2s["inputs_by_lang"], {"en": 2, "ja": 1, "ko": 1, "zh": 1})
        self.assertEqual(res["funnel"]["l2_profile_inputs"], 0)
        md = (self.home / "out" / "report.md").read_text()
        for needle in ("CNINFO 年度报告 2026-04-19 (zh)", "EDINET", "DART", "Annual-report excerpts by source"):
            self.assertIn(needle, md)
        with open(self.home / "out" / "results.csv", encoding="utf-8") as fh:
            self.assertIn("doc_lang", fh.readline())


class TestLanguageAndExcerpts(unittest.TestCase):
    def test_detect_language(self):
        d = screen.detect_language
        self.assertEqual(d(ZH_FULL), "zh")
        self.assertEqual(d(JA_YUHO), "ja")
        self.assertEqual(d(KO_DART), "ko")
        self.assertEqual(d(SEC_TEXT_ROBO), "en")
        self.assertEqual(d("", default="ja"), "ja")
        self.assertEqual(d("12345 ...", default="ko"), "ko")         # too little text: the source's language
        self.assertEqual(d(None), "en")
        # an English report quoting a few Chinese product names stays English
        self.assertEqual(d(SEC_TEXT_ROBO + " 人形机器人 "), "en")

    def test_cjk_excerpts_with_zh_keywords(self):
        ex = screen.build_excerpts(ZH_FULL, terms=["人形机器人"], lang="zh")
        self.assertEqual([e["kind"] for e in ex], ["overview", "keywords"])
        self.assertIn("工业自动化设备供应商", ex[0]["text"])
        self.assertIn("人形机器人", ex[1]["text"])
        self.assertTrue(all(len(e["text"]) <= screen.EXCERPT_MAX_CHARS_CJK for e in ex))
        # substring match inside a longer run of Han characters (no word boundaries in CJK)
        self.assertTrue(screen._term_pattern("机器人").search("公司研发并销售人形机器人"))
        # English terms do not help a Chinese document: only the overview
        self.assertEqual(len(screen.build_excerpts(ZH_FULL, terms=["humanoid"], lang="zh")), 1)

    def test_cjk_terms_match_without_the_models_spaces(self):
        text = ("公司主要提供面向企业的AI代理权限控制和身份管理解决方案，覆盖金融、政务和能源等行业客户，并提供持续的运维服务。\n\n"
                "公司的其他业务包括数据中心运维服务和系统集成服务，客户主要为大型国有企业和地方政府部门。")
        for term in ("AI 代理权限控制", "企业 AI 代理身份管理", "AI代理权限控制"):
            with self.subTest(term=term):
                pat = screen._term_pattern(term)
                if term == "企业 AI 代理身份管理":
                    self.assertIsNone(pat.search(text))
                else:
                    self.assertTrue(pat.search(text))
        self.assertTrue(screen._term_pattern("AI エージェント権限制御").search("当社はAIエージェント権限制御を提供"))
        self.assertTrue(screen._term_pattern("휴머노이드 로봇").search("휴머노이드로봇 매출"))
        self.assertIsNone(screen._term_pattern("AI 代理").search("MAI代理"))        # Latin boundary kept
        ex = screen.build_excerpts(text, terms=["AI 代理权限控制"], lang="zh")
        self.assertIn("keywords", [e["kind"] for e in ex])

    def test_cjk_overview_skips_figures_and_fills_free_slots(self):
        figures = "报告期内公司实现营业收入69,429.08万元，较去年同期减少2.57%；归属于上市公司股东的净利润为586.57万元，较去年同期增长107.59%。"
        biz = [f"公司第{i}项业务是面向银行和政府客户的身份认证与数字安全产品，包括智能密码钥匙、动态令牌和安全芯片，并提供配套的系统解决方案与服务。"
               for i in range(1, 16)]
        text = "\n\n".join([figures] + biz)
        self.assertTrue(screen.figure_heavy(figures))
        self.assertFalse(screen.figure_heavy(biz[0]))
        ex = screen.build_excerpts(text, terms=["人形机器人"], lang="zh")
        self.assertEqual([e["kind"] for e in ex], ["overview", "context", "context"])
        self.assertNotIn("营业收入", " ".join(e["text"] for e in ex))
        self.assertGreaterEqual(len(ex[0]["text"]), screen.OVERVIEW_MIN_CHARS_CJK)
        self.assertTrue(all(len(e["text"]) <= screen.EXCERPT_MAX_CHARS_CJK for e in ex))
        # English documents keep the old behaviour (no figure skipping, no context fill)
        self.assertEqual([e["kind"] for e in screen.build_excerpts("\n\n".join(["x" * 200] * 5), lang="en")],
                         ["overview"])

    def test_cninfo_full_report_excerpt_is_business_text(self):
        """On the (synthetic) 309386 FY2025 report the 主要业务 section opens with revenue / profit figures; layer 2 must get
        business text, and with the same-year summary first, the summary's business overview."""
        from jevscreen.sources import cninfo
        if not cninfo.pdf_backends():
            self.skipTest("no PDF backend installed")
        fx = Path(__file__).parent / "fixtures"
        full, _, _ = cninfo.extract_from_pdf((fx / "cninfo_309386_2025_full_p1-40.pdf").read_bytes(), "full")
        summ, _, _ = cninfo.extract_from_pdf((fx / "cninfo_309386_2025_summary.pdf").read_bytes(), "summary")
        self.assertTrue(full and summ)
        terms = ["AI代理权限控制", "企业 AI 代理身份管理"]      # generated-style phrases without a hit
        ex = screen.build_excerpts(full, terms=terms, lang="zh")
        self.assertFalse(ex[0]["text"].startswith("报告期内公司物流营业收入"))
        self.assertGreater(sum(len(e["text"]) for e in ex), 600)
        ex = screen.build_excerpts(summ + "\n\n" + full, terms=terms, lang="zh")
        self.assertTrue(ex[0]["text"].startswith("公司示例维护协同结构"), ex[0]["text"][:40])

    def test_flattened_edinet_block_is_split_into_sentence_chunks(self):
        paras = screen.split_paragraphs(JA_YUHO_FLAT, screen.EXCERPT_MIN_PARA_CJK, split_long=screen.EXCERPT_MAX_CHARS_CJK)
        self.assertGreaterEqual(len(paras), 5)
        self.assertTrue(all(len(p) <= screen.EXCERPT_MAX_CHARS_CJK for p in paras), [len(p) for p in paras])
        self.assertTrue(any(p.startswith("i. Sorano Pass") for p in paras))      # item markers start a chunk
        self.assertTrue(any(p.startswith("１ 【経営方針") and "】 （１）経営方針 当社グループは" in p for p in paras))  # heading kept
        self.assertNotIn("文中の将来に関する事項", " ".join(paras))                       # the yuho disclaimer is dropped
        self.assertIn("「Sorano Bridge」を併用すれば社内に残る旧来の業務システム", " ".join(paras))  # nothing else is lost
        # without split_long (English documents, short CJK paragraphs) the old behaviour holds
        self.assertEqual(len(screen.split_paragraphs(JA_YUHO_FLAT, screen.EXCERPT_MIN_PARA_CJK)), 2)

    def test_flattened_edinet_keyword_excerpt_holds_the_product_text(self):
        ex = screen.build_excerpts(JA_YUHO_FLAT, terms=JA_IAM_TERMS, lang="ja")
        kinds = [e["kind"] for e in ex]
        self.assertEqual(kinds, ["overview", "keywords", "context"])
        self.assertIn("子会社１社（Sorano Systems", ex[0]["text"])                # the overview is the business section's opening
        kw = ex[1]["text"]
        pats = [screen._term_pattern(t) for t in JA_IAM_TERMS]
        self.assertTrue(any(p.search(kw) for p in pats), kw)            # a keyword excerpt always holds a keyword
        self.assertIn("端末の状態に応じてアクセス制御の条件", kw)
        self.assertNotIn("文中の将来に関する事項", " ".join(e["text"] for e in ex))
        self.assertTrue(all(len(e["text"]) <= screen.EXCERPT_MAX_CHARS_CJK for e in ex))

    def test_keyword_window_in_one_long_cjk_paragraph(self):
        # one 1,000+ char run with no sentence ends to split at: the window must contain a hit and prefer the
        # densest cluster over the earliest lone hit
        text = ("背景" * 150 + " 「KumoGate One」は認証基盤" + "説明" * 200 + "アクセス制御と認証とID管理の機能" + "末尾" * 200)
        ex = screen.build_excerpts(text, terms=["認証", "アクセス制御", "ID管理"], lang="ja", max_n=2)
        kw = next(e["text"] for e in ex if e["kind"] == "keywords")
        self.assertIn("アクセス制御と認証とID管理", kw)
        self.assertLessEqual(len(kw), screen.EXCERPT_MAX_CHARS_CJK)
        # a lone early hit behind a far-away space: the window keeps the hit
        text = "前文" * 60 + " 「KumoGate One」" + "背景" * 200 + "認証基盤" + "末尾" * 300
        kw = next(e["text"] for e in screen.build_excerpts(text, terms=["認証"], lang="ja", max_n=2)
                  if e["kind"] == "keywords")
        self.assertIn("認証基盤", kw)

    def test_truncate_cjk_cuts_at_sentence_end_not_inside_latin_names(self):
        text = "当社はクラウドサービスを提供しております。" * 3 + "「KumoGate One」は認証基盤です。" + "続き" * 50
        cut = screen.truncate(text, 80, lang="ja")
        self.assertLessEqual(len(cut), 80)
        self.assertTrue(cut.endswith("。…"), cut)
        # no sentence end in reach: hard cut, never back to the space inside a product name (the space sits
        # past 60% of n, so a word-boundary cut would take it)
        text = "前" * 60 + "「KumoGate One」は認証基盤" + "後" * 100
        cut = screen.truncate(text, 80, lang="ja")
        self.assertEqual(len(cut), 80)
        self.assertIn("認証", cut)
        # English keeps the word-boundary cut
        self.assertEqual(screen.truncate("alpha beta gamma delta epsilon", 20), "alpha beta gamma…")

    def test_fullwidth_alphanumerics_match(self):
        tp = screen._term_pattern
        self.assertTrue(tp("ID管理").search("当社はＩＤ管理サービスを提供"))
        self.assertTrue(tp("APIセキュリティ").search("ＡＰＩセキュリティ製品"))
        self.assertTrue(tp("ＡＩエージェント").search("AIエージェントの導入"))
        self.assertTrue(tp("SAML").search("ＳＡＭＬ認証"))
        self.assertTrue(tp("AI").search("ＡＩを活用"))
        self.assertIsNone(tp("AI").search("ＭＡＩＬ"))                       # boundaries count full-width letters
        self.assertIsNone(tp("AI 代理").search("ＭAI代理"))

    def test_fullwidth_punctuation_in_terms_still_matches(self):
        tp = screen._term_pattern
        # a term matches its own exact text, whatever NFKC folds its punctuation to
        for term, doc in [("Ｍ＆Ａ", "当社はＭ＆Ａを推進"), ("M&A", "当社はＭ＆Ａを推進"), ("M&A", "当社はM&Aを推進"),
                          ("Ｒ＆Ｄ", "Ｒ＆Ｄ投資"), ("AI（人工知能）", "AI（人工知能）を活用"),
                          ("AI（人工知能）", "ＡＩ（人工知能）を活用"), ("IoT／AI", "IoT／AI基盤"),
                          ("IoT／AI", "ＩｏＴ／ＡＩ基盤"), ("Ｅ－コマース", "Ｅ－コマース事業"),
                          ("身份（ID）管理", "公司提供身份（ID）管理与权限控制"), ("ＩＤ管理（認証）", "当社はＩＤ管理（認証）を提供")]:
            self.assertTrue(tp(term).search(doc), (term, doc))

    def test_evidence_excerpt_of_a_keyword_hit_holds_a_term(self):
        # a whole <= 450-char chunk with the term past char 300: the 300-char evidence column must keep it
        head = "当社はクラウドサービスを提供しております。" * 14           # 294 chars, sentence ends throughout
        chunk = head + "主力製品は企業向けの認証基盤であり、多くの企業に採用されております。"
        text = "【事業の内容】\n\n" + chunk + "\n\n" + "当社の事業は順調に推移しております。" * 3
        terms = {"ja": ["認証基盤"]}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "doc.txt"
            p.write_text(text, encoding="utf-8")
            c = {"desc": {"plain": "", "text": "", "tier": None}}
            inp = screen._l2_input(c, {"text_path": str(p), "source_id": "edinet_yuho", "form": "yuho"}, terms,
                                   dt.date(2026, 9, 26))
        self.assertTrue(inp["keyword_hit"])
        self.assertIn("認証基盤", inp["evidence_excerpt"])
        self.assertLessEqual(len(inp["evidence_excerpt"]), screen.OUTPUT_EXCERPT_CHARS)
        # a sentence end before the hit is not taken when it drops the hit
        cut = screen._keyword_window(chunk, [screen._term_pattern("認証基盤")], 300, "ja")
        self.assertIn("認証基盤", cut)

    def test_split_chunks_do_not_break_quotes_or_enumerators(self):
        split = lambda t: screen.split_paragraphs(t, screen.EXCERPT_MIN_PARA_CJK, split_long=450)
        filler = "当社グループは事業を展開しております。" * 20
        text = (filler + " 報告セグメントの区分は「第５ 経理の状況 １ 連結財務諸表等 (1)連結財務諸表 連結財務諸表注記」に"
                "掲げるとおりであります。 " + filler + " 詳細は「（b）マテリアリティの特定 d. 指標と目標」をご参照ください。 "
                + filler)
        paras = split(text)
        self.assertFalse([p for p in paras if p.startswith(("(1)連結財務諸表 連結", "指標と目標」"))], paras)
        self.assertTrue(any("「第５ 経理の状況 １ 連結財務諸表等 (1)連結財務諸表 連結財務諸表注記」" in p for p in paras))
        self.assertTrue(any("「（b）マテリアリティの特定 d. 指標と目標」" in p for p in paras))
        # Korean enumerators (가. 나. 1.) start a chunk; they never end one
        ko = ("가. 업계의 현황 " + "국내 보안 시장은 꾸준히 성장하고 있습니다. " * 12 + "나. 회사의 현황 "
              + "당사는 인증 솔루션을 제공합니다. " * 16 + "1. 주요 제품 " + "당사의 제품은 다양합니다. " * 12)
        paras = split(ko)
        self.assertFalse([p for p in paras if re.search(r"(?:^|\s)(?:[가나다]|\d)\.$", p)], paras)
        self.assertTrue(any(p.startswith("나. 회사의 현황") for p in paras), paras)
        self.assertTrue(any(p.startswith("1. 주요 제품") for p in paras), paras)
        cut = screen.truncate("당사는 인증 솔루션을 제공합니다. " * 16 + "가. 업계의 현황 " + "설명 " * 50, 300, "ko")
        self.assertNotRegex(cut, r"\s가\.…$")

    def test_split_chunks_stay_under_the_cap(self):
        text = "【経営方針】 （１）当社は" + "あ" * 430 + "を行っております。 （２）当社は" + "い" * 300 + "。"
        chunks = screen._split_long_cjk(text, 450, 40)
        self.assertTrue(all(len(c) <= 450 for c in chunks), [len(c) for c in chunks])
        self.assertTrue(any(c.endswith("あを行っております。") for c in chunks))
        intro = "各種金融サービス事業や保険事業等の金融・保険分野において事業を行っている。"
        chunks = screen._split_long_cjk(intro + " " + "う" * 440 + "。 " + "え" * 200 + "。", 450, 40)
        self.assertTrue(all(len(c) <= 450 for c in chunks), [len(c) for c in chunks])

    def test_yuho_disclaimer_variants_are_dropped_with_their_connective(self):
        tail = "当社グループは、クラウドサービスを提供しております。" * 30
        for disc in ["なお、文中の将来に関する事項は、本書提出日現在において当社グループが判断したものであります。",
                     "文中における将来に関する事項は、当連結会計年度末現在において当社グループが判断したものです。",
                     "なお、文中における将来に関する事項は、当連結会計年度末現在において当社グループが判断したものです。",
                     "本項における将来に関する事項は、当連結会計年度末現在において判断したものであります。"]:
            paras = screen.split_paragraphs("（１）経営方針 " + disc + tail, 40, split_long=450)
            joined = " ".join(paras)
            self.assertNotIn("将来に関する事項", joined)
            self.assertNotIn("なお、", joined)
            self.assertTrue(paras[0].startswith("（１）経営方針 当社グループは、"), paras[0][:40])

    def test_keyword_window_scores_the_emitted_text(self):
        f = lambda n: ("lorem ipsum dolor sit amet " * 100)[:n].strip()
        terms = ["robot", "agent", "cloud", "platform"]
        pats = [screen._term_pattern(t) for t in terms]
        para = "Our robot business " + f(280) + " agent systems " + f(433) + " cloud platform " + f(400) + "."
        kw = screen._keyword_window(para, pats, 700, None)
        old = screen.truncate(para, 700)
        n = lambda s: len({t for t, p in zip(terms, pats) if p.search(s)})
        self.assertGreaterEqual(n(kw), n(old))
        self.assertTrue(kw.startswith("Our robot business"), kw[:30])   # ties keep the paragraph opening
        # a window start inside the first word falls back to the paragraph start
        para = "Supercalifragilisticexpialidocious " + " ".join(["filler"] * 33) + " platform " + " ".join(["tail"] * 200)
        kw = screen._keyword_window(para, [screen._term_pattern("platform")], 700, None)
        self.assertTrue(kw.startswith("Supercalifragilisticexpialidocious"), kw[:30])

    def test_terms_for_language(self):
        tb = {"en": ["humanoid"], "zh": ["人形机器人"], "ja": [], "ko": ["휴머노이드"]}
        self.assertEqual(screen.terms_for(tb, "zh"), ["人形机器人"])
        self.assertEqual(screen.terms_for(tb, "ja"), [])
        self.assertEqual(screen.terms_for(["a"], "en"), ["a"])
        self.assertEqual(screen.terms_for(["a"], "zh"), [])
        self.assertEqual(screen.cjk_idea_terms("企业 AI agent 的身份与权限管控", "zh"), ["权限管控", "企业", "身份"])
        self.assertEqual(screen.cjk_idea_terms("人形机器人", "ja"), [])       # a Chinese idea is not Japanese
        self.assertEqual(screen.cjk_idea_terms("ヒューマノイドロボット", "ja"), ["ヒューマノイドロボット"])
        self.assertEqual(screen.cjk_idea_terms("휴머노이드 로봇", "ko"), ["휴머노이드", "로봇"])


class TestAutoTranslation(StoreCase):
    def test_translated_idea_in_questions_and_outputs(self):
        res = self.run_screen(idea="人形机器人")
        self.assertEqual(self.kw.calls, ["人形机器人"])
        q1, q2 = self.log[0].classified[0][1], self.log[1].classified[0][1]
        for q in (q1, q2):
            self.assertIn('"Humanoid robots (original: 人形机器人)"', q.instructions)
        self.assertEqual(res["idea_en"], "Humanoid robots")
        self.assertEqual(res["terms"], ["humanoid robot"])
        self.assertEqual(res["terms_by_lang"]["zh"], ["人形机器人"])
        k = res["keywords"]
        self.assertEqual((k["status"], k["model"], k["cached"]), ("generated", "fake-qwen", False))
        self.assertIn("keywords_s", res["timing"])
        self.assertEqual(res["warnings"], [])
        # the generated English keyword finds the humanoid paragraph of the SEC 10-K
        robo = next(r for r in res["rows"] if r["security_id"] == "NYSE:ROBO")
        self.assertTrue(robo["l2_keyword_hit"])
        saved = json.loads((self.home / "out" / "results.json").read_text())
        self.assertEqual(saved["keywords"]["idea_en"], "Humanoid robots")
        md = (self.home / "out" / "report.md").read_text()
        for needle in ("## Idea and keywords", "Humanoid robots", "fake-qwen", "人形机器人", "translated by the local"):
            self.assertIn(needle, md)
        params = json.loads(self.query("SELECT params_json FROM screen_runs")[0][0])
        self.assertEqual((params["idea_en"], params["keywords_status"]), ("Humanoid robots", "generated"))

    def test_unavailable_keeps_old_behaviour_with_warning(self):
        kw = FakeKeywords(error=KeywordsUnavailable("model not in the HF cache"))
        res = self.run_screen(idea="企业 AI agent 的身份与权限管控", keywords_fn=kw)
        self.assertEqual(kw.calls, ["企业 AI agent 的身份与权限管控"])
        self.assertEqual(res["keywords"]["status"], "unavailable")
        self.assertIn("KeywordsUnavailable", res["keywords"]["error"])
        self.assertIsNone(res["idea_en"])
        self.assertIn('"企业 AI agent 的身份与权限管控"', self.log[0].classified[0][1].instructions)
        self.assertEqual(res["terms"], ["AI agent", "AI", "agent"])
        self.assertTrue(any("translation of the idea is unavailable" in w for w in res["warnings"]))
        self.assertTrue(any("--keywords" in w for w in res["warnings"]))

    def test_no_translate_and_english_keywords_skip_the_model(self):
        res = self.run_screen(idea="人形机器人", translate=False)
        self.assertEqual((self.kw.calls, res["keywords"]["status"], res["idea_en"]), ([], "disabled", None))
        self.assertTrue(res["warnings"])
        # English --keywords keep the English excerpt terms but no longer turn translation off
        res = self.run_screen(idea="人形机器人", keywords=["humanoid"], out_dir=self.home / "o2")
        self.assertEqual((self.kw.calls, res["keywords"]["status"], res["terms"], res["idea_en"]),
                         (["人形机器人"], "generated", ["humanoid"], "Humanoid robots"))
        self.assertTrue(res["keywords"]["user_keywords"])
        self.assertIn("Humanoid robots (original: 人形机器人)", self.log[-2].classified[0][1].instructions)
        self.kw.calls.clear()
        res = self.run_screen(idea="humanoid robots", out_dir=self.home / "o3")        # ASCII idea: no model
        self.assertEqual((self.kw.calls, res["keywords"]["status"]), ([], "not_needed"))
        res = self.run_screen(idea="humanoid robots", keywords=["humanoid"], out_dir=self.home / "o3b")
        self.assertEqual((self.kw.calls, res["keywords"]["status"]), ([], "user"))
        # per-language flags win over generated keywords
        # English ideas with smart quotes, dashes or accents are not translated and keep their question text
        for idea in ("humanoid robots’ supply chain", "AI agent — identity", "Nestlé suppliers"):
            res = self.run_screen(idea=idea, out_dir=self.home / f"o-{len(idea)}")
            self.assertEqual((self.kw.calls, res["keywords"]["status"], res["idea_en"]), ([], "not_needed", None))
            self.assertIn(f'"{idea}"', self.log[-2].classified[0][1].instructions)
        res = self.run_screen(idea="人形机器人", keywords_zh=["伺服"], out_dir=self.home / "o4")
        self.assertEqual(res["terms_by_lang"]["zh"], ["伺服"])
        self.assertEqual(res["terms_by_lang"]["ja"], ["ヒューマノイド"])

    def test_dry_run_generates_keywords(self):
        res = self.run_screen(idea="人形机器人", dry_run=True)
        self.assertEqual(self.kw.calls, ["人形机器人"])
        self.assertEqual(res["keywords"]["idea_en"], "Humanoid robots")
        text = report.format_console(res)
        self.assertIn("idea (en): Humanoid robots", text)
        self.assertIn("zh 人形机器人", text)

    def test_keywords_crash_is_contained(self):
        res = self.run_screen(idea="人形机器人", keywords_fn=FakeKeywords(error=RuntimeError("mps oom")))
        self.assertEqual(res["keywords"]["status"], "unavailable")
        self.assertEqual(res["status"], "ok")



class TestReport(unittest.TestCase):
    def test_missing_values_render_as_dash(self):
        self.assertEqual(report.money(None), "-")
        self.assertEqual(report.pct(None), "-")
        self.assertEqual(report.money(2.5e9), "2.5B")
        self.assertEqual(report._cell("a|b\nc"), "a\\|b c")



# ---------------------------------------------------------------------------------------------------------------
# Calibration plumbing: repeated L2 reads, --from-run, l2_inputs.jsonl, sieve keywords / rules / pins, --rank ev

GOLDEN_READS1 = Path(__file__).parent / "fixtures" / "screen_reads1_golden.json"
OLD_CSV_COLUMNS = ("rank", "security_id", "company_key", "name", "country", "region", "market_cap_usd",
                   "revenue_ttm_usd", "revenue_cagr_3y_local", "cagr_currency", "cagr_years", "l1_label", "l1_p_core",
                   "l2_label", "l2_p_top", "l2_status", "l2_evidence", "score", "evidence_excerpt", "evidence_url",
                   "filing_source", "filing_form", "filing_date", "doc_lang", "l2_doc_stale", "l2_keyword_hit",
                   "l1_input_tier", "l2_input_tier", "l2_input_source", "l1_request_id", "l2_request_id")


def golden_snapshot(official: bool, **extra) -> dict:
    """A single-read screen on the test seed, reduced to everything that existed before repeated reads: the Jev calls
    (questions, item texts and item keys), rows, CSV and screen_results in the old columns, funnel, layers, gaps.
    tests/fixtures/screen_reads1_golden.json was captured with this function from the code before this change.
    To regenerate it after a seed-text edit: run this function (no extra arguments) against the pre-calibration code
    (commit 7c80218's src/ with the edited seed) for {"sec": False, "official": True}, write it with
    json.dump(..., ensure_ascii=False, indent=1, sort_keys=True) plus a newline, and check that the current code with
    reads=1 gives the identical file."""
    from jevscreen import jev
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        cfg = config.Config(home=home)
        seed(cfg, home)
        if official:
            seed_official(cfg, home)
        log: list = []
        extra.setdefault("l1_rescue", 0)     # the golden predates l1_rescued (P0-2): GearCo's L1 miss is not read
        res = screen.screen(cfg, "人形机器人" if official else "humanoid robots", jev_factory=make_factory(log),
                            out_dir=home / "out", keywords_fn=FakeKeywords(), **extra)
        with open(home / "out" / "results.csv", encoding="utf-8") as fh:
            csv_rows = [{k: r[k] for k in OLD_CSV_COLUMNS} for r in csv.DictReader(fh)]
        with store.session(cfg, read_only=True) as con:
            db = [list(r) for r in con.execute(
                "SELECT company_key, security_id, layer, label, probs_json, p_top, request_id, input_source, "
                "input_tier, evidence_url, evidence_excerpt, status, error FROM screen_results "
                "ORDER BY layer, company_key").fetchall()]
    calls = [{"layer": c.layer, "read": getattr(q, "read", 0),
              "question": {"key": q.key, "instructions": q.instructions, "criteria": q.criteria},
              "items": [[it.item_id, it.issuer, it.text, jev.item_key(q, it.issuer, it.text)] for it in items]}
             for c in log for items, q in c.classified]
    layers = {k: {kk: vv for kk, vv in v.items() if kk not in ("seconds", "band")} for k, v in res["layers"].items()}
    return json.loads(json.dumps({
        "status": res["status"], "funnel": res["funnel"], "layers": layers, "cost_usd": res["cost_usd"],
        "questions": res["questions"], "terms_by_lang": res["terms_by_lang"], "gaps": res["gaps"],
        "rows": [{k: r.get(k) for k in OLD_CSV_COLUMNS} for r in res["rows"]],
        "unverified": [{k: r.get(k) for k in OLD_CSV_COLUMNS} for r in res["unverified"]],
        "csv": csv_rows, "db": db, "calls": calls, "tiers_used": res["tiers_used"], "warnings": res["warnings"]},
        ensure_ascii=False, sort_keys=True, default=str))


# read -> probabilities per company for BandJev (other items / reads: the keyword labels of FakeJev)
BAND_TABLE = {
    # Robo Two: insufficient on read 0 (p_pos 0.55, in the band), partial on the mean of reads 0-2 (p_pos 0.6167)
    ("isin:US0000000007", 0): {"explicit": 0.2, "partial": 0.35, "contradicted": 0.05, "insufficient": 0.4},
    ("isin:US0000000007", 1): {"explicit": 0.3, "partial": 0.5, "contradicted": 0.05, "insufficient": 0.15},
    ("isin:US0000000007", 2): {"explicit": 0.2, "partial": 0.3, "contradicted": 0.1, "insufficient": 0.4},
    # ServoJP (profile): stays insufficient, mean p_pos 0.4333 -> 边缘
    ("isin:JP0000000002", 0): {"explicit": 0.1, "partial": 0.35, "contradicted": 0.05, "insufficient": 0.5},
    ("isin:JP0000000002", 1): {"explicit": 0.1, "partial": 0.3, "contradicted": 0.1, "insufficient": 0.5},
    ("isin:JP0000000002", 2): {"explicit": 0.1, "partial": 0.35, "contradicted": 0.05, "insufficient": 0.5},
}
FRESH = {"explicit": 0.6, "partial": 0.3, "contradicted": 0.05, "insufficient": 0.05}   # reads >= 3 (read_offset)


class BandJev(FakeJev):
    """FakeJev whose L2 answers come from BAND_TABLE by (item_id, question.read); records every (read, item order)
    and asserts that no L1 question is ever sent when `no_l1` is set (from_run)."""

    def __init__(self, cfg, *, no_l1=False, **kw):
        super().__init__(cfg, **kw)
        self.no_l1, self.reads_seen = no_l1, []

    def classify(self, items, question):
        assert not (self.no_l1 and question.key == "fit"), "from_run must not send an L1 question"
        read = getattr(question, "read", 0)
        self.reads_seen.append((read, [it.item_id for it in items]))
        out = super().classify(items, question)
        if question.key != "fit":
            for x in out:
                probs = BAND_TABLE.get((x["item_id"], read)) or (FRESH if read >= 3 and (x["item_id"], 0) in BAND_TABLE
                                                                  else None)
                if probs and x["status"] == "ok":
                    x.update(probs=dict(probs), label=max(probs, key=probs.get))
        return out


def band_factory(log, **kw):
    def factory(cfg, *, run_id, layer, budget_usd, dry_run, **extra):
        return BandJev(cfg, run_id=run_id, layer=layer, budget_usd=budget_usd, dry_run=dry_run, log=log, **kw)
    return factory


class TestRepeatedReads(StoreCase):
    def run_band(self, **kw):
        kw.setdefault("jev_factory", band_factory(self.log))
        return self.run_screen(**kw)

    def test_reads1_output_is_identical_to_before(self):
        golden = json.loads(GOLDEN_READS1.read_text(encoding="utf-8"))
        for name, official in (("sec", False), ("official", True)):
            with self.subTest(seed=name):
                self.assertEqual(golden_snapshot(official, reads=1), golden[name])
        # the default (3 reads) changes nothing when no item is in the band
        self.assertEqual(golden_snapshot(False), golden["sec"])

    def test_band_items_get_more_reads_and_are_decided_on_the_mean(self):
        res1 = self.run_band(reads=1, out_dir=self.home / "r1")
        self.assertEqual([r["security_id"] for r in res1["rows"]], ["NYSE:ROBO"])      # Robo Two insufficient
        self.log.clear()
        res = self.run_band(reads=3, l1_rescue=0)
        l2 = self.log[1]
        self.assertEqual([r for r, _ in l2.reads_seen], [0, 1, 2])
        for read, ids in l2.reads_seen[1:]:
            self.assertEqual(set(ids), {"isin:US0000000007", "isin:JP0000000002"})    # only the band
            self.assertEqual(ids, sorted(ids, key=lambda i: screen.hashlib.sha256(f"{read}:{i}".encode()).hexdigest()))
        rows = {r["security_id"]: r for r in res["rows"]}
        self.assertEqual(list(rows), ["NYSE:ROBO", "NASDAQ:ROB2"])
        rob2 = rows["NASDAQ:ROB2"]
        self.assertEqual((rob2["l2_label"], rob2["l2_reads"], rob2["l2_edge"], rob2["verdict_source"]),
                         ("partial", 3, False, "evidence"))
        self.assertAlmostEqual(rob2["l2_p_pos"], (0.55 + 0.8 + 0.5) / 3, places=4)
        self.assertAlmostEqual(rob2["l2_p_explicit"], 0.7 / 3, places=4)
        self.assertAlmostEqual(rob2["l2_p_partial"], 1.15 / 3, places=4)
        self.assertGreater(rob2["l2_p_pos_sd"], 0)
        self.assertEqual([d["read"] for d in rob2["l2_read_detail"]], [0, 1, 2])
        robo = rows["NYSE:ROBO"]
        self.assertEqual((robo["l2_reads"], robo["l2_p_pos_sd"], robo["l2_edge"]), (1, 0.0, False))
        servo = res["unverified"][0]
        self.assertEqual((servo["security_id"], servo["l2_label"], servo["l2_reads"], servo["l2_edge"]),
                         ("TSE:6000", "insufficient", 3, True))
        band = res["layers"]["l2"]["band"]
        self.assertEqual((band["items"], band["planned"], band["ok"], band["skipped"], band["edge"]), (2, 4, 4, 0, 1))
        self.assertEqual(res["layers"]["l2"]["labels"], {"explicit": 1, "insufficient": 1, "partial": 1})
        self.assertEqual(res["status"], "ok")
        # screen_results keeps the mean and the reads
        reads_json, p_pos, label = self.query(
            "SELECT reads_json, p_pos, label FROM screen_results WHERE run_id = ? AND layer = 'l2' "
            "AND security_id = 'NASDAQ:ROB2'", [res["run_id"]])[0]
        self.assertEqual(([d["read"] for d in json.loads(reads_json)], label), ([0, 1, 2], "partial"))
        self.assertAlmostEqual(p_pos, (0.55 + 0.8 + 0.5) / 3)
        with open(self.home / "out" / "results.csv", encoding="utf-8") as fh:
            csv_rows = {r["security_id"]: r for r in csv.DictReader(fh)}
        self.assertEqual((csv_rows["NASDAQ:ROB2"]["l2_reads"], csv_rows["NASDAQ:ROB2"]["l2_edge"]), ("3", "False"))
        # a rerun of the same configuration asks for exactly the same reads (cached in real Jev)
        self.log.clear()
        self.run_band(reads=3, out_dir=self.home / "again", l1_rescue=0)
        self.assertEqual(self.log[1].reads_seen, l2.reads_seen)

    def test_band_order_changes_per_read_and_is_deterministic(self):
        items = [screen._FallbackItem(item_id=f"c{i:02d}", issuer=f"C{i}", text="t") for i in range(12)]
        res = {it.item_id: {"item_id": it.item_id, "status": "ok", "label": "partial",
                            "probs": {"explicit": 0.1, "partial": 0.4, "insufficient": 0.5}} for it in items}
        res["c00"]["probs"] = {"explicit": 0.9, "partial": 0.05, "insufficient": 0.05}      # outside the band

        class Rec:
            spent_usd = requests_sent = 0

            def __init__(self):
                self.calls = []

            def classify(self, its, q):
                self.calls.append((q.read, [it.item_id for it in its]))
                return [{"item_id": it.item_id, "status": "ok", "label": "partial", "cached": False,
                         "probs": {"explicit": 0.1, "partial": 0.4, "insufficient": 0.5}} for it in its]

        q = screen._FallbackQuestion(key="evidence", instructions="i", criteria={"explicit": "e"})
        a, b = Rec(), Rec()
        out = screen._l2_band_reads(a, items, res, q, 3)
        screen._l2_band_reads(b, items, res, q, 3)
        self.assertEqual(a.calls, b.calls)
        (r1, o1), (r2, o2) = a.calls
        self.assertEqual((r1, r2), (1, 2))
        self.assertNotIn("c00", o1)
        self.assertEqual(sorted(o1), sorted(o2))
        self.assertNotEqual(o1, o2)
        self.assertEqual((out["planned"], out["ok"], out["skipped"]), (22, 22, 0))
        self.assertEqual(q.read, 0)                                   # the base question is not changed
        self.assertEqual(screen.band_read_indices(3), [1, 2])
        self.assertEqual(screen.band_read_indices(3, 3), [3, 4, 5])
        self.assertEqual(screen.band_read_indices(1), [])

    def test_aggregate_reads(self):
        agg = screen.aggregate_reads([
            (0, {"status": "ok", "probs": {"explicit": 0.4, "partial": 0.2, "insufficient": 0.4}, "request_id": "a"}),
            (1, {"status": "failed", "probs": {}, "request_id": None}),
            (2, {"status": "ok", "probs": {"explicit": 0.2, "partial": 0.4, "insufficient": 0.4}, "request_id": "b",
                 "cached": True})])
        self.assertEqual(agg["n"], 2)
        self.assertEqual(agg["label"], "insufficient")     # mean 0.3 / 0.3 / 0.4
        self.assertAlmostEqual(agg["p_explicit"], 0.3)
        self.assertAlmostEqual(agg["probs"]["insufficient"], 0.4)
        self.assertAlmostEqual(agg["p_pos"], 0.6)
        self.assertAlmostEqual(agg["p_pos_sd"], 0.0)
        self.assertEqual([(d["read"], d["cached"]) for d in agg["reads"]], [(0, False), (1, False), (2, True)])
        tie = screen.aggregate_reads([(0, {"status": "ok", "probs": {"insufficient": 0.5, "partial": 0.5}})])
        self.assertEqual(tie["label"], "partial")      # ties -> L2_CRITERIA order
        self.assertIsNone(screen.aggregate_reads([(0, {"status": "failed", "probs": {}})]))

    def test_short_budget_reads_once(self):
        # L1 $0.01 + L2 read 0 $0.01: nothing left for the band reads
        res = self.run_band(reads=3, budget_usd=0.02)
        self.assertEqual(res["status"], "partial")
        rob2 = next(r for r in res["unverified"] if r["security_id"] == "NASDAQ:ROB2")
        self.assertEqual((rob2["l2_label"], rob2["l2_reads"], rob2["l2_read_note"]),
                         ("insufficient", 1, screen.READ_ONCE_NOTE))
        self.assertEqual(res["layers"]["l2"]["band"]["skipped"], 4)
        res = self.run_band(reads=3, budget_usd=0.03, out_dir=self.home / "o2")    # read 1 only
        rob2 = next(r for r in res["rows"] + res["unverified"] if r["security_id"] == "NASDAQ:ROB2")
        self.assertEqual((rob2["l2_reads"], rob2["l2_read_note"]), (2, "只读了 2/3 次"))

    def test_read_offset_replaces_read_0_of_the_band(self):
        res = self.run_band(reads=3, read_offset=3)
        self.assertEqual([r for r, _ in self.log[1].reads_seen], [0, 3, 4, 5])
        rob2 = next(r for r in res["rows"] if r["security_id"] == "NASDAQ:ROB2")
        self.assertEqual([d["read"] for d in rob2["l2_read_detail"]], [3, 4, 5])
        self.assertEqual((rob2["l2_label"], rob2["l2_reads"]), ("explicit", 3))
        self.assertAlmostEqual(rob2["l2_p_pos"], 0.9)
        robo = next(r for r in res["rows"] if r["security_id"] == "NYSE:ROBO")
        self.assertEqual([d["read"] for d in robo["l2_read_detail"]], [0])     # outside the band: read 0 stays

    def test_dry_run_estimate_includes_band_reads(self):
        res = self.run_screen(dry_run=True, reads=3)
        est = res["layers"]["l2"]["estimate"]
        self.assertEqual(est["band_reads"]["extra_reads"], 2)
        self.assertAlmostEqual(est["est_cost_usd"], 0.01 * (1 + 2 * screen.BAND_SHARE_ESTIMATE))
        res = self.run_screen(dry_run=True, reads=1, out_dir=self.home / "d1")
        self.assertNotIn("band_reads", res["layers"]["l2"]["estimate"])

    def test_bad_read_parameters(self):
        for kw in ({"reads": 0}, {"reads": True}, {"read_offset": -1}, {"rank": "best"}):
            with self.subTest(**kw), self.assertRaises(ValueError):
                self.run_screen(**kw)


# P0-2 (novice simulation #2): 申菱 301018's TradingView profile spoke only of air conditioning and was cut off; L1
# said unrelated 60% / adjacent 36% / core 2% and the annual report, where the energy-storage business is, was never
# read. The rescued L1 misses are read by L2 on their annual report.
SHENLING_PROFILE = "Shenling Environmental makes precision air conditioning units for data centres, rail transit and"
SHENLING_AR = """第三节 管理层讨论与分析

一、报告期内公司从事的主要业务

公司主要从事人工环境调控设备的研发、生产和销售，产品用于数据中心、轨道交通和工业厂房。

报告期内，公司人形机器人关节热管理产品实现批量供货，人形机器人业务收入同比增长。
"""


def l1_label_rescue(text: str, _base=l1_label) -> tuple[str, dict[str, float]]:
    if "air conditioning" in text.lower():
        return "unrelated", {"core": 0.02, "adjacent": 0.36, "unrelated": 0.60, "insufficient": 0.02}
    return _base(text)


def seed_shenling(cfg: config.Config, home: Path, *, with_doc: bool = True) -> None:
    docs = home / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "shenling.txt").write_text(SHENLING_AR, encoding="utf-8")
    with store.session(cfg) as con:
        snap = lambda src: store.record_snapshot(  # noqa: E731
            con, source_id=src, kind="t", request=None, raw_path=None, raw_sha256=None, raw_bytes=None, rows=None,
            duration_s=None)
        scan, prof, cn = snap("tradingview_scanner"), snap("tradingview_profile"), snap("cninfo_annual_report")
        store.upsert_many(con, "securities", SEC_COLS, [
            ("SZSE:301018", "SZSE", "301018", "Shenling", "CNE100004HY4", "China", "stock", "common", True,
             "isin:CNE100004HY4", scan, True)])
        store.upsert_many(con, "market_daily", ("security_id", "as_of", "market_cap_usd", "avg_volume_10d",
                                                "snapshot_id"), [("SZSE:301018", D, 1.5e9, 1e6, scan)])
        store.upsert_many(con, "descriptions", ("security_id", "source_id", "company_key", "text", "snapshot_id"),
                          [("SZSE:301018", "tradingview_profile", "isin:CNE100004HY4", SHENLING_PROFILE, prof)])
        if with_doc:
            store.upsert_many(con, "documents", (
                "doc_id", "security_id", "company_key", "source_id", "cik", "form", "section", "filing_date",
                "report_date", "url", "text_path", "text_chars", "extract_note", "snapshot_id"), [
                ("cninfo_annual_report:9900301018:f25:business", "SZSE:301018", "isin:CNE100004HY4",
                 "cninfo_annual_report", "9900301018", "年度报告", "business", dt.date(2026, 4, 20),
                 dt.date(2025, 12, 31), "http://www.cninfo.com.cn/shenling.pdf", str(docs / "shenling.txt"), 200,
                 None, cn)])


class TestL1Rescue(StoreCase):
    def run_rescue(self, **kw):
        with mock.patch.object(sys.modules[__name__], "l1_label", l1_label_rescue):
            return self.run_screen(**kw)

    def test_a_thin_profile_l1_miss_is_listed_on_its_annual_report(self):
        seed_shenling(self.cfg, self.home)
        res = self.run_rescue()
        rows = {r["security_id"]: r for r in res["rows"]}
        self.assertIn("SZSE:301018", rows)
        sl = rows["SZSE:301018"]
        self.assertEqual((sl["l1_label"], sl["l2_label"], sl["l2_evidence"], sl["l1_rescued"]),
                         ("unrelated", "explicit", "annual_report", True))
        self.assertNotIn("l1_rescued", rows["NYSE:ROBO"])
        self.assertIn("unrelated (rescued)", (self.home / "out" / "report.md").read_text(encoding="utf-8"))
        from jevscreen import page
        self.assertIn("l1_rescued", page._badges(sl, None))
        self.assertNotIn("l1_rescued", page._badges(rows["NYSE:ROBO"], None))
        # GearCo (gearbox, core + adjacent 0.40) is read too, on its profile only: never listed, never 'unverified'
        self.assertEqual(res["layers"]["l2"]["l1_rescued"], {"read": 2, "listed": 1})
        self.assertNotIn("LSE:GEAR", [r["security_id"] for r in res["rows"] + res["unverified"]])
        self.assertIn("LSE:GEAR", [g["security_id"] for g in res["gaps"]["l2_profile_only"]])
        _h, lines = screen.read_ledger(self.home / "out")
        by = {ln["id"]: ln for ln in lines}
        self.assertTrue(by["SZSE:301018"]["l1"]["rs"])
        self.assertFalse(by["SZSE:301018"]["l1"]["ok"])
        self.assertNotIn("rs", by["NYSE:ROBO"]["l1"])
        (pj,) = self.query("SELECT params_json FROM screen_runs WHERE run_id = ?", [res["run_id"]])[0]
        self.assertEqual(sorted(json.loads(pj)["l1_rescued"]), ["isin:CNE100004HY4", "isin:GB0000000003"])

    def test_the_page_says_how_many_listed_companies_were_rescued(self):
        from jevscreen import page
        seed_shenling(self.cfg, self.home)
        res = self.run_rescue()
        for lang, needle in (("zh", "有 1 家初读没通过"), ("en", "1 of the listed companies did not pass the first read")):
            data = page.build_page_data(res, None, lang=lang)
            self.assertEqual(data["funnel"]["rescued"], 1)
            self.assertIn(needle, page.render_text(data, lang))
        self.assertNotIn("初读没通过", page.render_text(page.build_page_data(self.run_rescue(
            l1_rescue=0, out_dir=self.home / "off"), None, lang="zh"), "zh"))

    def test_without_the_rescue_it_stops_at_l1(self):
        seed_shenling(self.cfg, self.home)
        res = self.run_rescue(l1_rescue=0)
        self.assertNotIn("SZSE:301018", [r["security_id"] for r in res["rows"]])
        self.assertNotIn("l1_rescued", res["layers"]["l2"])

    def test_profile_only_rescue_goes_to_the_fetch_after_the_passes(self):
        from jevscreen import ondemand
        seed_shenling(self.cfg, self.home, with_doc=False)
        res = self.run_rescue()
        self.assertNotIn("SZSE:301018", [r["security_id"] for r in res["rows"] + res["unverified"]])
        self.assertIn("SZSE:301018", [g["security_id"] for g in res["gaps"]["l2_profile_only"]])
        params = {"l1_rescued": ["isin:CNE100004HY4"]}
        inp = {"company_key": "isin:CNE100004HY4", "l1_label": "unrelated",
               "l1_probs": json.dumps({"core": 0.02, "adjacent": 0.36, "unrelated": 0.6})}
        self.assertFalse(ondemand._is_forced(inp, params))          # after the L1 passes, not before them
        self.assertTrue(ondemand._is_forced(inp, {}))               # a sieve check that missed L1: first

    def test_a_free_rerank_keeps_the_rescued_company(self):
        seed_shenling(self.cfg, self.home)
        res = self.run_rescue()
        self.assertIn("SZSE:301018", [r["security_id"] for r in res["rows"]])
        again = self.run_rescue(from_run=res["run_id"], rank_only=True, out_dir=self.home / "rank")
        self.assertIn("SZSE:301018", [r["security_id"] for r in again["rows"]])     # judge / decide / reapply
        from jevscreen import calib
        with store.session(self.cfg, read_only=True) as con:
            pool = {c["security_id"]: c for c in calib.load_pool(con, res["run_id"], res["params"])}
        self.assertTrue(pool["SZSE:301018"]["l1_rescued"])
        self.assertNotIn("l1_rescued", pool["LSE:GEAR"])               # profile only: not listable

    def test_a_check_on_a_rescued_company_keeps_it_listed(self):
        seed_shenling(self.cfg, self.home)
        sieve = humanoid_sieve(examples=[{"security_id": "SZSE:301018", "want": "explicit", "source": "sieve",
                                          "pin": False}])
        res = self.run_rescue(sieve=sieve, reads=1)
        self.assertIn("SZSE:301018", [r["security_id"] for r in res["rows"]])
        self.assertEqual(res["layers"]["l2"]["l1_rescued"]["listed"], 1)

    def test_no_rescue_while_l1_passes_wait_beyond_l2_max(self):
        seed_shenling(self.cfg, self.home)
        res = self.run_rescue(l2_max=1)
        self.assertNotIn("l1_rescued", res["layers"]["l2"])
        self.assertEqual(res["funnel"]["l2_sent"], 1)

    def test_why_explains_a_listed_rescued_company(self):
        from jevscreen import why
        seed_shenling(self.cfg, self.home)
        res = self.run_rescue()
        out = why.run(self.cfg, ["SZSE:301018"], run_ref=res["run_id"])["results"][0]
        text = json.dumps(out, ensure_ascii=False)
        self.assertIn("年报原文", text)
        self.assertNotIn("add_should_pass", [c.get("id") for c in out.get("changes") or []])

    def test_the_cap_and_the_order(self):
        by_key = {f"k{i}": {"security_id": f"X:{i}", "market_cap_usd": 1e9 + i,
                            "desc": {"plain": "A full profile sentence that ends properly. " * 12}} for i in range(5)}
        res = {f"k{i}": {"status": "ok", "label": "unrelated",
                         "probs": {"core": 0.0, "adjacent": 0.25 + 0.05 * i, "unrelated": 0.7, "insufficient": 0.0}}
               for i in range(5)}
        got = screen.l1_rescued(res, by_key, set(), adjacent_min=0.6, core_min=0.0, limit=2)
        self.assertEqual(got, ["k4", "k3"])                             # 0.45, 0.40 (k0 at 0.25 is below 0.30)
        self.assertEqual(screen.l1_rescued(res, by_key, {"k4"}, adjacent_min=0.6, core_min=0.0), ["k3", "k2", "k1"])
        # a thin profile adds the model's "cannot tell"
        r = {"status": "ok", "probs": {"core": 0.0, "adjacent": 0.1, "unrelated": 0.5, "insufficient": 0.4}}
        self.assertAlmostEqual(screen.l1_rescue_score(r, "Short."), 0.5)
        self.assertAlmostEqual(screen.l1_rescue_score(r, "A full profile sentence that ends properly. " * 12), 0.1)
        self.assertTrue(screen.profile_thin("x" * 500 + " and"))             # cut off mid-sentence
        self.assertTrue(screen.profile_thin("x" * 500 + "…"))
        self.assertFalse(screen.profile_thin("公司主要从事储能温控设备的研发和销售。" * 30))
        self.assertIsNone(screen.l1_rescue_score({"status": "failed"}, "x"))

    def test_a_stored_report_outranks_a_kept_profile_only_rescue(self):
        by_key = {k: {"security_id": f"X:{k}", "market_cap_usd": 1e9, "desc": {"plain": "Short."}}
                  for k in ("kept", "new")}
        res = {k: {"status": "ok", "label": "unrelated",
                   "probs": {"core": 0.0, "adjacent": 0.4, "unrelated": 0.6, "insufficient": 0.0}} for k in by_key}
        kw = {"adjacent_min": 0.6, "core_min": 0.0, "limit": 1, "keep": ["kept"]}
        self.assertEqual(screen.l1_rescued(res, by_key, set(), has_doc=["new"], **kw), ["new"])   # new evidence
        self.assertEqual(screen.l1_rescued(res, by_key, set(), **kw), ["kept"])        # no new evidence: kept
        self.assertEqual(screen.l1_rescued(res, by_key, set(), has_doc=["new", "kept"], **kw), ["kept"])

    def test_a_later_version_reads_a_rescued_miss_whose_report_arrived(self):
        seed_shenling(self.cfg, self.home, with_doc=False)
        base = self.run_rescue(l1_rescue=1, out_dir=self.home / "base")
        self.assertEqual(base["params"]["l1_rescued"], ["isin:GB0000000003"])       # GearCo: profile only
        seed_shenling(self.cfg, self.home)                                           # Shenling's report arrives
        res = self.run_rescue(from_run=base["run_id"], l1_rescue=1)
        self.assertEqual(res["params"]["l1_rescued"], ["isin:CNE100004HY4"])
        self.assertIn("SZSE:301018", [r["security_id"] for r in res["rows"]])

    def test_from_run_and_the_update_pass_inherit_the_rescue_setting(self):
        seed_shenling(self.cfg, self.home)
        base = self.run_rescue(l1_rescue=0, out_dir=self.home / "base")
        for kw in ({}, {"supersedes": base["run_id"], "budget_usd": 0.05}):
            with self.subTest(**kw):
                res = self.run_rescue(from_run=base["run_id"], out_dir=self.home / "base", **kw)
                self.assertEqual(res["params"]["l1_rescue"], 0)
                self.assertNotIn("l1_rescued", res["layers"]["l2"])
                self.assertNotIn("SZSE:301018", [r["security_id"] for r in res["rows"]])

    def test_a_check_that_is_also_rescued_is_fetched_first(self):
        from jevscreen import ondemand
        seed_shenling(self.cfg, self.home, with_doc=False)
        sieve = humanoid_sieve(examples=[{"security_id": "SZSE:301018", "want": "explicit", "source": "sieve",
                                          "pin": False}])
        res = self.run_rescue(sieve=sieve, reads=1)
        self.assertIn("isin:CNE100004HY4", res["params"]["l1_rescued"])             # a check, and rescued too
        pl = ondemand.plan(self.cfg, res["run_id"])
        self.assertTrue(pl.entries["isin:CNE100004HY4"]["forced"])
        self.assertEqual(pl.order[0], "isin:CNE100004HY4")

    def test_the_fetch_takes_rescued_misses_after_the_passes(self):
        from jevscreen import ondemand

        def l1(text: str, _base=l1_label) -> tuple[str, dict[str, float]]:
            t = text.lower()
            if "air conditioning" in t:     # a rescued miss whose p(core) is above a pass's
                return "unrelated", {"core": 0.25, "adjacent": 0.1, "unrelated": 0.6, "insufficient": 0.05}
            if "servo" in t:                # an adjacent pass with a low p(core)
                return "adjacent", {"core": 0.05, "adjacent": 0.6, "unrelated": 0.3, "insufficient": 0.05}
            return _base(text)
        seed_shenling(self.cfg, self.home, with_doc=False)
        with mock.patch.object(sys.modules[__name__], "l1_label", l1):
            res = self.run_screen()
        self.assertEqual(sorted(res["params"]["l1_rescued"]), ["isin:CNE100004HY4", "isin:GB0000000003"])
        pl = ondemand.plan(self.cfg, res["run_id"])
        self.assertEqual(pl.order, ["isin:JP0000000002", "isin:CNE100004HY4", "isin:GB0000000003"])
        self.assertEqual([pl.entries[k].get("rescued") for k in pl.order], [False, True, True])

    def test_fetch_texts_and_questions_keep_rescued_misses_apart(self):
        from jevscreen import ondemand
        seed_shenling(self.cfg, self.home, with_doc=False)
        with store.session(self.cfg) as con:         # a US L1 miss with a cut-off profile: rescued, no 10-K stored
            snap = con.execute("SELECT snapshot_id FROM snapshots WHERE source_id = 'tradingview_profile' LIMIT 1"
                               ).fetchone()[0]
            store.upsert_many(con, "securities", SEC_COLS, [
                ("NYSE:COOL", "NYSE", "COOL", "CoolAir", "US0000000009", "United States", "stock", "common", True,
                 "isin:US0000000009", snap, True)])
            store.upsert_many(con, "market_daily", ("security_id", "as_of", "market_cap_usd", "avg_volume_10d",
                                                    "snapshot_id"), [("NYSE:COOL", D, 2e9, 1e6, snap)])
            store.upsert_many(con, "descriptions", ("security_id", "source_id", "company_key", "text",
                                                    "snapshot_id"),
                              [("NYSE:COOL", "tradingview_profile", "isin:US0000000009",
                                "CoolAir makes air conditioning units for offices and", snap)])
        res = self.run_rescue()
        self.assertIn("isin:US0000000009", res["params"]["l1_rescued"])
        pl = ondemand.plan(self.cfg, res["run_id"])
        self.assertEqual(pl.skip_counts().get("no_key_sec"), 1)                    # only CoolAir needs the SEC
        self.assertEqual(ondemand.questions_for(pl, pl.skip_counts()), [])          # it missed step 1: not asked
        zh, en = ondemand.start_line(pl, "zh", 120), ondemand.start_line(pl, "en", 120)
        self.assertIn("4 家公司本地没有年报原文（通过第一轮的 1 家", zh)
        self.assertIn("3 家", zh)
        self.assertIn("4 companies have no annual-report text stored (1 passed the first round, 3 missed it", en)

    def test_no_band_or_facet_reads_for_a_rescued_miss_read_on_its_profile(self):
        def l2(text: str, _base=l2_label) -> tuple[str, dict[str, float]]:
            if "air conditioning" in text.lower():     # the profile reads as related (in the band)
                return "partial", {"explicit": 0.2, "partial": 0.4, "contradicted": 0.1, "insufficient": 0.3}
            return _base(text)
        seed_shenling(self.cfg, self.home, with_doc=False)
        with mock.patch.object(sys.modules[__name__], "l2_label", l2):
            res = self.run_rescue(reads=3, facet_scan=True, sieve="none")
        self.assertIn("isin:CNE100004HY4", res["params"]["l1_rescued"])
        extra = [it.item_id for c in self.log if c.layer in ("l2", "facet") for items, q in c.classified
                 if getattr(q, "read", 0) or q.key.startswith("facet_") for it in items]
        self.assertTrue(res["facets"])                                  # the facet layer ran for the others
        self.assertNotIn("isin:CNE100004HY4", extra)
        self.assertNotIn("isin:CNE100004HY4", res["facets"])
        self.assertIn("SZSE:301018", [g["security_id"] for g in res["gaps"]["l2_profile_only"]])


class TestFromRun(StoreCase):
    def base(self, **kw):
        kw.setdefault("jev_factory", band_factory(self.log))
        return self.run_screen(translate=False, keywords=["humanoid robot"], max_out=5, reads=1,
                               out_dir=self.home / "base", **kw)

    def test_l1_is_loaded_not_read_and_params_inherited(self):
        base = self.base()
        self.log.clear()
        res = self.run_screen(from_run=base["run_id"], jev_factory=band_factory(self.log, no_l1=True),
                              out_dir=self.home / "next")
        self.assertEqual([c.layer for c in self.log], ["l2"])              # no L1 client at all
        l1 = res["layers"]["l1"]
        self.assertEqual((l1["cost_usd"], l1["requests"], l1["from_run"], l1["loaded"]), (0.0, 0, base["run_id"], 5))
        self.assertEqual(res["funnel"]["l1_pass"], base["funnel"]["l1_pass"])
        self.assertEqual({r["security_id"]: (r["l1_label"], r["l1_p_core"], r["l1_request_id"]) for r in res["rows"]},
                         {r["security_id"]: (r["l1_label"], r["l1_p_core"], r["l1_request_id"]) for r in base["rows"]})
        p = res["params"]
        self.assertEqual((p["translate"], p["keywords"], p["max_out"], p["reads"], p["from_run"]),
                         (False, ["humanoid robot"], 5, 1, base["run_id"]))
        self.assertEqual(self.kw.calls, [])                                # translate=False inherited
        self.assertIn("L1 loaded from run", " ".join(res["notes"]))
        # the new run stores its own L1 rows, so it can be a base run itself
        self.assertEqual(self.query("SELECT count(*) FROM screen_results WHERE run_id = ? AND layer = 'l1'",
                                    [res["run_id"]])[0][0], 5)
        self.log.clear()
        res2 = self.run_screen(from_run=res["run_id"], max_out=1, reads=3,
                               jev_factory=band_factory(self.log, no_l1=True), out_dir=self.home / "third")
        self.assertEqual((res2["params"]["max_out"], res2["params"]["reads"], len(res2["rows"])), (1, 3, 1))
        self.assertEqual(res2["params"]["keywords"], ["humanoid robot"])

    def test_companies_missing_from_the_base_run_do_not_pass(self):
        base = self.base()
        with store.session(self.cfg) as con:
            con.execute("DELETE FROM screen_results WHERE run_id = ? AND security_id = 'NASDAQ:ROB2'", [base["run_id"]])
        res = self.run_screen(from_run=base["run_id"], jev_factory=band_factory(self.log, no_l1=True),
                              out_dir=self.home / "next")
        self.assertEqual([r["security_id"] for r in res["rows"]], ["NYSE:ROBO"])
        self.assertIn("1 described companies have no L1 answer", " ".join(res["notes"]))

    def test_unknown_or_other_idea_is_refused(self):
        base = self.base()
        with self.assertRaises(ValueError):
            self.run_screen(from_run="scr-nope")
        with self.assertRaisesRegex(ValueError, "another idea"):
            self.run_screen(idea="servo motors", from_run=base["run_id"])
        dry = self.run_screen(dry_run=True, out_dir=self.home / "dry")
        with store.session(self.cfg) as con:      # a dry run writes no screen_runs row: fake one without L1 rows
            store.upsert_many(con, "screen_runs", screen.RUN_COLS, [
                ("scr-dry", "humanoid robots", "{}", None, None, "dry_run", 0) + (None,) * 7])
        with self.assertRaisesRegex(ValueError, "no stored L1"):
            self.run_screen(from_run="scr-dry")
        self.assertEqual(dry["status"], "dry_run")

    def test_dry_run_from_run_estimates_the_real_passes(self):
        base = self.base()
        res = self.run_screen(from_run=base["run_id"], dry_run=True, out_dir=self.home / "d")
        # the real passes, plus the base run's rescued L1 misses (GearCo: gearbox, core + adjacent 0.4)
        self.assertEqual(res["layers"]["l2"]["inputs"],
                         base["funnel"]["l1_pass"] + base["layers"]["l2"]["l1_rescued"]["read"])
        self.assertEqual(base["layers"]["l2"]["l1_rescued"], {"read": 1, "listed": 0})
        self.assertEqual(res["layers"]["l1"]["estimate"]["est_cost_usd"], 0.0)


class TestL2InputsAndExcerpts(StoreCase):
    def test_l2_inputs_jsonl_and_evidence_sha(self):
        res = self.run_screen()
        lines = [json.loads(x) for x in (self.home / "out" / "l2_inputs.jsonl").read_text().splitlines()]
        self.assertEqual(len(lines), res["funnel"]["l2_inputs"])
        self.assertEqual(set(lines[0]), {"company_key", "security_id", "evidence", "source_id", "lang", "text",
                                         "excerpts", "keyword_hit", "matched_terms", "evidence_sha"})
        robo = next(x for x in lines if x["security_id"] == "NYSE:ROBO")
        self.assertTrue(robo["keyword_hit"])
        self.assertTrue(robo["matched_terms"])
        self.assertTrue(set(robo["matched_terms"]) <= set(res["terms_by_lang"]["en"]))
        self.assertEqual(robo["evidence_sha"], screen.evidence_sha(robo["text"]))
        row = next(r for r in res["rows"] if r["security_id"] == "NYSE:ROBO")
        self.assertEqual(row["evidence_sha"], robo["evidence_sha"])
        # the tag line does not count: a new filing date alone keeps the sha
        body = robo["text"].split("\n", 1)[1]
        self.assertEqual(screen.evidence_sha("[annual report excerpts: SEC 10-K filed 2027-01-01; language en]\n"
                                             + body), robo["evidence_sha"])
        self.assertNotEqual(screen.evidence_sha(robo["text"] + " more"), robo["evidence_sha"])
        self.assertEqual(len(robo["evidence_sha"]), 16)
        self.assertFalse((self.home / "dry").exists())
        self.run_screen(dry_run=True, out_dir=self.home / "dry")
        self.assertFalse((self.home / "dry" / "l2_inputs.jsonl").exists())

    def test_short_cjk_keyword_fragment_is_widened(self):
        overview = "当社グループは、工場自動化向けの制御機器と産業用ソフトウェアの開発・製造・販売を主な事業としております。" * 7
        before = "当社は、国内外の製造業のお客様に対して、生産ラインの設計から保守までを一貫して提供しております。"
        frag = "当社はクラウド向けのシングルサインオン製品を開発し、国内の企業のお客様に販売しております。"
        after = "今後は海外の販売代理店網を拡充し、アジア地域でのサービス提供体制を一層強化してまいります。"
        text = "\n\n".join([overview, before, frag, after])
        self.assertTrue(screen.EXCERPT_MIN_PARA_CJK <= len(frag) < screen.CJK_FRAGMENT_MIN_CHARS, len(frag))
        self.assertTrue(all(len(p) >= screen.EXCERPT_MIN_PARA_CJK for p in (before, after)))
        ex = screen.build_excerpts(text, terms=["シングルサインオン"], lang="ja")
        kw = next(e["text"] for e in ex if e["kind"] == "keywords")
        self.assertGreaterEqual(len(kw), screen.CJK_FRAGMENT_MIN_CHARS)
        self.assertIn(frag, kw)
        self.assertIn(before, kw)
        self.assertLessEqual(len(kw), screen.EXCERPT_MAX_CHARS_CJK)
        joined = " ".join(e["text"] for e in ex)
        self.assertEqual(joined.count(after[:20]), 1)                  # the neighbours are not repeated later
        # without neighbours to take (a figure-heavy one is skipped) the fragment stays as it is
        figures = "報告期間の売上高は12,345百万円、営業利益は1,234百万円、前期比12.3%の増加となりました。"
        ex = screen.build_excerpts("\n\n".join([overview, frag, figures]), terms=["シングルサインオン"], lang="ja")
        self.assertEqual(next(e["text"] for e in ex if e["kind"] == "keywords"), frag)

    def test_weak_terms(self):
        noise = "当社は、情報セキュリティマネジメントシステムの認証を取得し、品質管理体制の強化を継続しております。"
        sso = "当社は、クラウドサービス向けのシングルサインオン製品を開発し、企業のお客様に提供しております。"
        both = "当社は、多要素認証とシングルサインオンを組み合わせた認証基盤を開発し、企業のお客様に提供しております。"
        overview = "当社グループは、企業向けソフトウェアの開発・販売を主な事業としております。" * 9
        terms = ["認証", "シングルサインオン"]
        # a weak-only paragraph is never the keyword paragraph
        ex = screen.build_excerpts("\n\n".join([overview, noise]), terms=terms, lang="ja", weak_terms=["認証"])
        self.assertNotIn("keywords", [e["kind"] for e in ex])
        ex = screen.build_excerpts("\n\n".join([overview, noise]), terms=terms, lang="ja")
        self.assertIn(noise, next(e["text"] for e in ex if e["kind"] == "keywords"))
        # a weak term still breaks a tie between normal hits
        ex = screen.build_excerpts("\n\n".join([overview, sso, both]), terms=terms, lang="ja", weak_terms=["認証"])
        self.assertIn(both, next(e["text"] for e in ex if e["kind"] == "keywords"))
        ex = screen.build_excerpts("\n\n".join([overview, sso, both]), terms=["シングルサインオン"], lang="ja")
        self.assertIn(sso, next(e["text"] for e in ex if e["kind"] == "keywords"))      # earliest without it

    def test_resolve_keywords_with_sieve_keywords(self):
        kw = dict(keywords=None, keywords_by_lang={"ja": ["認証", "ID管理"]}, translate=False, keywords_fn=None)
        plain = screen.resolve_keywords(self.cfg, "IAM for agents", **kw)
        self.assertEqual(plain["weak"], {})
        sieve_kw = {"ja": {"add": ["シングルサインオン", "SAML", "ID管理"], "weak": ["認証"], "log": []},
                    "xx": {"add": ["ignored"]}}
        info = screen.resolve_keywords(self.cfg, "IAM for agents", sieve_kw=sieve_kw, **kw)
        self.assertEqual(info["terms"]["ja"], ["ID管理", "シングルサインオン", "SAML"])      # appended, deduplicated
        self.assertEqual(info["weak"], {"ja": ["認証"]})
        self.assertEqual(info["terms"]["en"], plain["terms"]["en"])
        self.assertNotIn("xx", info["terms"])


def humanoid_sieve(**kw) -> dict:
    return {"format": "jevscreen.sieve/1", "version": 2, "idea": "humanoid robots", "idea_key": "k", "facets": None,
            "rules": [], "examples": [], "history": [], "decks": [], "rejected_rules": [], **kw}


class TestSieveInScreen(StoreCase):
    def test_pins_forced_l2_and_checks(self):
        sieve = humanoid_sieve(examples=[
            {"security_id": "LSE:GEAR", "want": "explicit", "chip": "a", "source": "card", "pin": True},
            {"security_id": "NYSE:ROBO", "want": "no", "chip": "c", "source": "card", "pin": True},
            {"security_id": "TSE:6000", "want": "partial", "chip": "b", "source": "card", "pin": True},
            {"security_id": "NASDAQ:BANK", "want": "explicit", "source": "sieve", "pin": False},
            {"security_id": "NASDAQ:ROB2", "want": "unsure", "source": "card", "pin": False}])
        res = self.run_screen(sieve=sieve, reads=1)
        l2_ids = {it.item_id for it in self.log[1].classified[0][0]}
        self.assertTrue({"isin:GB0000000003", "isin:US0000000004"} <= l2_ids)      # failed L1, read anyway
        rows = {r["security_id"]: r for r in res["rows"]}
        self.assertEqual(list(rows), ["NASDAQ:ROB2", "LSE:GEAR", "TSE:6000"])
        gear, servo, rob2 = rows["LSE:GEAR"], rows["TSE:6000"], rows["NASDAQ:ROB2"]
        self.assertEqual((gear["user_verdict"], gear["verdict_source"], gear["l2_label"], gear["l2_forced"]),
                         ("explicit", "user", "insufficient", True))
        self.assertEqual(gear["user_note"], "用户判断（年报未写明）")
        self.assertEqual(gear["score"], round(screen.score_of("explicit", 0.1, 3e9, "profile"), 4))
        self.assertEqual((servo["user_verdict"], servo["verdict_source"]), ("partial", "user"))
        self.assertEqual((rob2["user_verdict"], rob2["verdict_source"], rob2["backfill"]), (None, "evidence", False))
        self.assertEqual([r["security_id"] for r in res["excluded_by_user"]], ["NYSE:ROBO"])
        self.assertEqual(res["excluded_by_user"][0]["verdict_source"], "user")
        self.assertEqual(res["unverified"], [])
        cal = res["calibration"]
        self.assertEqual((cal["answers"], cal["pins"], cal["excluded"], cal["user_rows"]), (4, 3, 1, 2))
        self.assertEqual([f["security_id"] for f in cal["forced"]], ["LSE:GEAR", "NASDAQ:BANK"])
        self.assertEqual(cal["checks"], [{"security_id": "NASDAQ:BANK", "company_key": "isin:US0000000004",
                                          "name": "BankCo", "want": "explicit", "in_universe": True,
                                          "l1_pass": False, "l2_label": "insufficient", "rank": None,
                                          "in_output": False, "extra_reads": 2,        # should_pass: v1.1
                                          # BankCo is profile-only: wanted at most partial (review v1.1)
                                          "flag_zh": "应通过：多读 2 次，仍低于「相关」（仅简介，最多算相关）"}])
        self.assertEqual(cal["summary_zh"], "已加载校准：4 条回答，0 条规则")
        self.assertEqual(res["layers"]["l2"]["forced"], 2)
        self.assertEqual(res["params"]["sieve_path"], "<inline>")
        with open(self.home / "out" / "results.csv", encoding="utf-8") as fh:
            gear_csv = next(r for r in csv.DictReader(fh) if r["security_id"] == "LSE:GEAR")
        self.assertEqual((gear_csv["user_verdict"], gear_csv["verdict_source"], gear_csv["backfill"]),
                         ("explicit", "user", "False"))

    def test_backfill(self):
        res = self.run_screen(sieve=humanoid_sieve(examples=[
            {"security_id": "NYSE:ROBO", "want": "no", "source": "card", "pin": True}]), max_out=1)
        self.assertEqual([(r["security_id"], r["backfill"]) for r in res["rows"]], [("NASDAQ:ROB2", True)])
        self.assertEqual(res["calibration"]["backfill"], 1)
        res = self.run_screen(max_out=1, out_dir=self.home / "o2")        # no sieve: no pins, no backfill
        self.assertEqual([(r["security_id"], r["backfill"]) for r in res["rows"]], [("NYSE:ROBO", False)])
        self.assertNotIn("excluded_by_user", res)
        self.assertNotIn("calibration", res)

    def test_rules_keywords_and_auto_sieve_file(self):
        from jevscreen import calib
        sieve = humanoid_sieve(rules=[{"id": "mention_only", "version": 1}],
                               keywords={"en": {"add": ["warehouse picking"], "weak": [], "log": []}})
        path = calib.sieve_path(self.cfg, "humanoid robots")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sieve), encoding="utf-8")
        res = self.run_screen(sieve="auto")
        q2 = self.log[1].classified[0][1]
        self.assertTrue(q2.criteria["insufficient"].startswith(
            screen.L2_CRITERIA["insufficient"] + " A single mention"))
        self.assertEqual(q2.criteria["explicit"], screen.L2_CRITERIA["explicit"])
        self.assertEqual(res["questions"]["l2"]["criteria"], q2.criteria)
        self.assertIn("warehouse picking", res["terms_by_lang"]["en"])
        p = res["params"]
        self.assertEqual((p["sieve_path"], p["sieve_version"]), (str(path), 2))
        self.assertEqual(p["sieve_sha256"], screen.hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(res["calibration"]["rules"], ["mention_only"])
        self.assertEqual(res["calibration"]["keywords_add"], {"en": ["warehouse picking"]})
        # no file for another idea: auto means no sieve; an explicit missing path or a bad rule is an error
        res = self.run_screen(idea="servo motors", sieve="auto", out_dir=self.home / "o2")
        self.assertNotIn("calibration", res)
        with self.assertRaises(ValueError):
            self.run_screen(sieve=self.home / "missing.json")
        with self.assertRaises(ValueError):
            self.run_screen(sieve=humanoid_sieve(rules=[{"id": "no_such_rule"}]))
        self.assertEqual(self.query("SELECT count(*) FROM screen_runs WHERE idea = 'humanoid robots'")[0][0], 1)

    def test_build_l2_question_rules(self):
        plain = screen.build_l2_question("idea x")
        self.assertEqual(screen.build_l2_question("idea x", rules=()), plain)
        q = screen.build_l2_question("idea x", rules=["mention_only", "laundry_list", "mention_only"])
        self.assertEqual(q, screen.build_l2_question("idea x", rules=["laundry_list", "mention_only"]))
        ins = q.criteria["insufficient"]
        self.assertLess(ins.index("A service provider"), ins.index("A single mention"))   # sorted by rule id
        self.assertEqual(ins.count("A single mention"), 1)
        self.assertEqual(screen._question_dict(plain), {"key": plain.key, "instructions": plain.instructions,
                                                        "criteria": plain.criteria})


class TestRankEv(StoreCase):
    def test_ev_scores_on_mean_probabilities(self):
        res = self.run_screen(rank="ev", jev_factory=band_factory(self.log))
        self.assertEqual(res["params"]["rank"], "ev")
        for r in res["rows"]:
            want = screen.score_ev(r["l2_p_explicit"], r["l2_p_partial"], r["l1_p_core"], r["market_cap_usd"],
                                   r["l2_evidence"])
            self.assertAlmostEqual(r["score"], want, places=3)
        self.assertAlmostEqual(screen.score_ev(1.0, 0.0, 0.5, 1e9), screen.score_of("explicit", 0.5, 1e9))
        self.assertAlmostEqual(screen.score_ev(0.0, 1.0, 0.5, 1e9, "profile"),
                               screen.score_of("partial", 0.5, 1e9, "profile"))


if __name__ == "__main__":
    unittest.main()
