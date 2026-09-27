"""Tests for jevscreen.cli and jevscreen.coverage. No network: adapters are faked and Client.request is disabled."""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

import duckdb  # noqa: E402

try:  # duckdb may load numpy lazily inside mock.patch.dict(sys.modules), which then drops it from sys.modules and
    import numpy  # noqa: F401  # numpy refuses a second load in this process (breaks later edgartools imports)
except ImportError:
    pass

from jevscreen import cli, coverage, store  # noqa: E402
from jevscreen.http import Blocked  # noqa: E402

D1, D2 = dt.date(2026, 9, 25), dt.date(2026, 9, 26)
SEC_COLS = ("security_id", "exchange", "symbol", "name", "isin", "country", "tv_type", "tv_subtype", "is_primary",
            "company_key", "last_seen_snapshot", "active")


def _populate(con) -> None:
    snap = lambda src, kind: store.record_snapshot(  # noqa: E731
        con, source_id=src, kind=kind, request=None, raw_path=None, raw_sha256=None, raw_bytes=None, rows=None,
        duration_s=None)
    scan, prof, fd = snap("tradingview_scanner", "scan"), snap("tradingview_profile", "page"), \
        snap("financedatabase_local", "import")
    sec_snap = snap("sec_filing_text", "annual_report")

    def sec(sid, name, isin, country, tv_type="stock", sub="common", primary=True, active=True):
        ex, sym = sid.split(":")
        return (sid, ex, sym, name, isin, country, tv_type, sub, primary, store.company_key(isin, sid), scan, active)

    store.upsert_many(con, "securities", SEC_COLS, [
        sec("TSE:7203", "Toyota", "JP3633400001", "Japan"),
        sec("NYSE:BABA", "Alibaba ADR", "US01609W1027", "China", tv_type="dr", sub=None),
        sec("LSE:SHEL", "Shell", "GB00BP6MXD84", "United Kingdom"),       # two lines, one company
        sec("AMS:SHELL", "Shell", "GB00BP6MXD84", "Netherlands"),
        sec("NASDAQ:AAPL", "Apple", "US0378331005", "United States"),
        sec("XYZ:NOCAP", "No Cap Co", None, None),
        sec("AMEX:SPY", "SPDR", "US78462F1030", "United States", tv_type="fund", sub="etf"),   # not in universe
        sec("NYSE:OLD", "Delisted", None, "United States", active=False),                    # not in universe
        sec("NYSE:PRF", "Pref", None, "United States", sub="preferred"),                      # not in universe
    ])
    mcols = ("security_id", "as_of", "market_cap_usd", "snapshot_id")
    store.upsert_many(con, "market_daily", mcols, [
        ("TSE:7203", D1, 1.0e11, scan), ("TSE:7203", D2, 2.2e11, scan), ("NYSE:BABA", D2, 3.0e11, scan),
        ("LSE:SHEL", D2, 2.0e11, scan), ("AMS:SHELL", D2, 1.9e11, scan), ("NASDAQ:AAPL", D2, 3.5e12, scan),
        ("AMEX:SPY", D2, 5.0e11, scan)])
    fcols = ("security_id", "as_of", "metric", "period", "value_usd", "value_local", "currency_local", "snapshot_id")
    store.upsert_many(con, "fundamentals_current", fcols, [
        ("TSE:7203", D2, "total_revenue", "ttm", 3.2e11, 5.2e13, "JPY", scan),
        ("NYSE:BABA", D2, "total_revenue", "ttm", None, 9.9e11, "CNY", scan),
        ("NASDAQ:AAPL", D1, "total_revenue", "ttm", 4.0e11, 4.0e11, "USD", scan),
        ("NASDAQ:AAPL", D2, "total_revenue", "ttm", None, None, "USD", scan),     # latest is NULL -> not covered
        ("LSE:SHEL", D2, "net_income", "ttm", 1.0e10, 7.0e9, "GBP", scan),        # other metric -> not covered
    ])
    acols = ("security_id", "fiscal_year", "metric", "value_local", "currency_local", "snapshot_id")
    annual = [("TSE:7203", y, "total_revenue", 1e13, "JPY", scan) for y in range(2014, 2026)]
    annual += [("NYSE:BABA", y, "total_revenue", 1e12, "CNY", scan) for y in (2023, 2024, 2025)]
    annual += [("NYSE:BABA", 2022, "total_revenue", None, "CNY", scan)]           # NULL stays NULL, not a year
    annual += [("LSE:SHEL", y, "total_revenue", 2e11, "USD", scan) for y in (2024, 2025)]
    annual += [("NASDAQ:AAPL", y, "net_income", 1e11, "USD", scan) for y in range(2010, 2026)]
    store.upsert_many(con, "fundamentals_annual", acols, annual)
    dcols = ("security_id", "source_id", "company_key", "text", "snapshot_id")
    store.upsert_many(con, "descriptions", dcols, [
        ("TSE:7203", "tradingview_profile", "isin:JP3633400001", "Toyota makes cars.", prof),
        ("TSE:7203", "financedatabase_local", "isin:JP3633400001", "Toyota Motor Corporation ...", fd),
        ("NYSE:BABA", "financedatabase_local", "isin:US01609W1027", "Alibaba ...", fd),
        ("AMS:SHELL", "tradingview_profile", "isin:GB00BP6MXD84", "Shell plc ...", prof),  # other line, same company
        ("XYZ:NOCAP", "tradingview_profile", "sec:XYZ:NOCAP", "   ", prof),               # blank -> not covered
    ])
    docols = ("doc_id", "security_id", "company_key", "source_id", "cik", "form", "section", "text_path", "snapshot_id")
    store.upsert_many(con, "documents", docols, [
        ("sec_filing_text:1577552:a1:item4", "NYSE:BABA", "isin:US01609W1027", "sec_filing_text", "1577552", "20-F",
         "item4", "/docs/baba-item4.txt", sec_snap),
        ("sec_filing_text:320193:a2:item1", "NASDAQ:AAPL", "isin:US0378331005", "sec_filing_text", "320193", "10-K",
         "item1", None, sec_snap),                                                   # no extracted text -> not counted
    ])


class CoverageReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.con = duckdb.connect(":memory:")
        store.init(self.con)
        _populate(self.con)
        self.rep = coverage.report(self.con)

    def tearDown(self) -> None:
        self.con.close()

    def test_totals(self) -> None:
        self.assertEqual(self.rep["totals"], {"companies": 5, "market_cap": 4, "ttm_revenue": 2, "revenue_fy3": 2,
                                              "revenue_fy10": 1, "description": 3, "sec_document": 1,
                                              "official_document": 1})

    def test_regions(self) -> None:
        by = {r: v["companies"] for r, v in self.rep["by_region"].items()}
        self.assertEqual(by, {"US": 1, "China": 1, "Japan": 1, "Europe & UK": 1, "Other": 1})
        self.assertEqual(self.rep["by_region"]["Europe & UK"]["description"], 1)
        self.assertEqual(self.rep["by_region"]["Other"]["market_cap"], 0)

    def test_descriptions_by_source(self) -> None:
        self.assertEqual(self.rep["description_by_source"],
                         {"financedatabase_local": 2, "sec_filing_text": 0, "tradingview_profile": 2})
        self.assertEqual(self.rep["sec_document_by_form"], {"20-F": 1})
        self.assertEqual(self.rep["by_region"]["China"]["sec_document"], 1)
        gaps = self.rep["gaps"]["top_market_cap_without_description"]
        self.assertFalse(gaps[0]["has_sec_document"])

    def test_license_tiers_computed(self) -> None:
        tiers = self.rep["license_tiers"]
        self.assertEqual(set(tiers), {"gray-private", "official-private"})
        self.assertEqual(tiers["gray-private"]["companies"], 5)
        self.assertEqual(tiers["official-private"]["companies"], 2)   # documents rows (with or without text)
        self.assertGreater(tiers["gray-private"]["rows"], 5)

    def test_null_snapshot_description_reported_as_unknown_tier(self) -> None:
        store.upsert_many(self.con, "descriptions", ["security_id", "source_id", "company_key", "text", "snapshot_id"],
                          [["NASDAQ:AAPL", "x", "isin:US0378331005", "Some text", None]])
        rep = coverage.report(self.con)
        self.assertEqual(rep["description_by_source"].get("x"), 1)
        self.assertEqual(rep["license_tiers"]["unknown"]["rows"], 1)

    def test_freshness_and_gaps(self) -> None:
        f = self.rep["freshness"]
        self.assertEqual(f["market_as_of"], "2026-09-26")
        self.assertEqual(set(f["latest_snapshot"]),
                         {"tradingview_scanner", "tradingview_profile", "financedatabase_local", "sec_filing_text"})
        gaps = self.rep["gaps"]["top_market_cap_without_description"]
        self.assertEqual([g["security_id"] for g in gaps], ["NASDAQ:AAPL"])

    def test_official_documents_by_source_and_region(self) -> None:
        snap = lambda src: store.record_snapshot(  # noqa: E731
            self.con, source_id=src, kind="t", request=None, raw_path=None, raw_sha256=None, raw_bytes=None,
            rows=None, duration_s=None)
        cn, ja, ko = snap("cninfo_annual_report"), snap("edinet_yuho"), snap("dart_business_report")
        store.upsert_many(self.con, "securities", SEC_COLS, [
            ("SZSE:300386", "SZSE", "300386", "Feitian", "CNE100001TR1", "China", "stock", "common", True,
             "isin:CNE100001TR1", None, True),
            ("KRX:005930", "KRX", "005930", "Samsung", "KR7005930003", "South Korea", "stock", "common", True,
             "isin:KR7005930003", None, True)])
        store.upsert_many(self.con, "identifiers", ["security_id", "id_type", "id_value"],
                          [["KRX:005930", "dart_corp_code", "00126380"]])
        cols = ["doc_id", "security_id", "company_key", "source_id", "cik", "form", "text_path", "snapshot_id"]
        store.upsert_many(self.con, "documents", cols, [
            ["cninfo_annual_report:9900002701:1:business", "SZSE:300386", "isin:CNE100001TR1",
             "cninfo_annual_report", "9900002701", "年度报告摘要", "/docs/cn.txt", cn],
            ["edinet_yuho:E02144:S100:business", None, "isin:JP3633400001", "edinet_yuho", "E02144",
             "有価証券報告書", "/docs/ja.txt", ja],
            ["edinet_yuho:E02144:S099:business", None, "isin:JP3633400001", "edinet_yuho", "E02144",
             "有価証券報告書", None, ja],                                         # no text: not counted twice
            ["dart_business_report:00126380:2026:business", None, None, "dart_business_report", "00126380",
             "사업보고서", "/docs/ko.txt", ko],                                   # only via dart_corp_code
        ])
        rep = coverage.report(self.con)
        self.assertEqual(rep["official_document_by_source"], {"cninfo_annual_report": 1, "dart_business_report": 1,
                                                               "edinet_yuho": 1, "sec_filing_text": 1,
                                                               "mops_annual_report": 0, "bse_annual_report": 0})
        self.assertEqual(rep["official_document_by_region_source"],
                         {"China": {"cninfo_annual_report": 1, "sec_filing_text": 1}, "Japan": {"edinet_yuho": 1},
                          "South Korea": {"dart_business_report": 1}})
        self.assertEqual((rep["totals"]["official_document"], rep["totals"]["sec_document"]), (4, 1))
        self.assertEqual(rep["by_region"]["Japan"]["official_document"], 1)
        self.assertEqual(rep["license_tiers"]["official-private"]["companies"], 4)
        text = coverage.format_report(rep)
        for token in ("official_doc", "by region and source", "cninfo_annual_report", "South Korea"):
            self.assertIn(token, text)

    def test_format_report(self) -> None:
        text = coverage.format_report(self.rep)
        for token in ("TOTAL", "Europe & UK", "gray-private", "NASDAQ:AAPL", "3.5T", "sec_doc", "20-F"):
            self.assertIn(token, text)

    def test_region_of(self) -> None:
        self.assertEqual(coverage.region_of(None), "Other")
        self.assertEqual(coverage.region_of("Atlantis"), "Other")
        self.assertEqual(coverage.region_of("Hong Kong"), "Hong Kong")
        self.assertEqual(coverage.region_of("Brazil"), "Latin America")
        self.assertEqual(coverage.region_of("Israel"), "Middle East & Africa")
        self.assertEqual(set(coverage.REGION_OF_COUNTRY.values()) | {"Other"}, set(coverage.REGIONS))
        self.assertTrue(set(coverage.REGION_OF_EXCHANGE.values()) <= set(coverage.REGIONS))

    def test_live_country_names_mapped(self) -> None:
        expected = {
            "Pakistan": "South Asia (other)", "Bangladesh": "South Asia (other)", "Sri Lanka": "South Asia (other)",
            "Russian Federation": "Europe & UK", "Aland Islands": "Europe & UK", "Faroe Islands": "Europe & UK",
            "Ireland": "Europe & UK", "Luxembourg": "Europe & UK", "Czech Republic": "Europe & UK",
            "Nigeria": "Middle East & Africa", "Sudan": "Middle East & Africa", "Togo": "Middle East & Africa",
            "United Arab Emirates": "Middle East & Africa", "Vietnam": "Southeast Asia", "Cambodia": "Southeast Asia",
            "Macau": "Hong Kong", "Kazakhstan": "Other Asia", "Mongolia": "Other Asia", "Azerbaijan": "Other Asia",
            "Bermuda": "Offshore / Caribbean", "Cayman Islands": "Offshore / Caribbean", "Guam": "Offshore / Caribbean",
            "Puerto Rico": "Offshore / Caribbean", "British Virgin Islands": "Offshore / Caribbean",
        }
        self.assertEqual({c: coverage.region_of(c) for c in expected}, expected)

    def test_region_for_uses_exchange_for_offshore_and_null(self) -> None:
        rf = coverage.region_for
        self.assertEqual(rf("Cayman Islands", "NASDAQ"), "US")
        self.assertEqual(rf("Bermuda", "HKEX"), "Hong Kong")
        self.assertEqual(rf("British Virgin Islands", "LSE"), "Europe & UK")
        self.assertEqual(rf("Cayman Islands", "TPEX"), "Taiwan")
        self.assertEqual(rf(None, "TSE"), "Japan")
        self.assertEqual(rf(None, "PSX"), "South Asia (other)")
        self.assertEqual(rf("Atlantis", "NYSE"), "US")                 # unmapped country -> venue
        self.assertEqual(rf("Japan", "NASDAQ"), "Japan")               # a real country always wins
        self.assertEqual(rf("Bermuda", "XYZ"), "Offshore / Caribbean")  # unknown venue -> incorporation bucket
        self.assertEqual(rf(None, None), "Other")
        self.assertEqual(rf(None, "XYZ"), "Other")

    def test_report_regions_from_exchange(self) -> None:
        con = duckdb.connect(":memory:")
        store.init(con)
        snap = store.record_snapshot(con, source_id="tradingview_scanner", kind="scan", request=None, raw_path=None,
                                     raw_sha256=None, raw_bytes=None, rows=None, duration_s=None)
        rows = [("NASDAQ:CAYM", "Cayman Islands"), ("HKEX:9999", "Bermuda"), ("TSE:1111", None),
                ("XYZ:ZZZ", "Bermuda"), ("XYZ:NUL", None)]
        store.upsert_many(con, "securities", SEC_COLS, [
            (sid, sid.split(":")[0], sid.split(":")[1], sid, None, c, "stock", "common", True,
             store.company_key(None, sid), snap, True) for sid, c in rows])
        rep = coverage.report(con)
        con.close()
        by = {r: v["companies"] for r, v in rep["by_region"].items()}
        self.assertEqual(by, {"US": 1, "Hong Kong": 1, "Japan": 1, "Offshore / Caribbean": 1, "Other": 1})


def _fake_module(name: str, **funcs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in funcs.items():
        setattr(mod, k, v)
    return mod


class _CliBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        env = {"JEVSCREEN_HOME": self.tmp.name, "JEVSCREEN_MIN_INTERVAL_S": "1.0"}
        for p in (mock.patch.dict(os.environ, env),
                  mock.patch("jevscreen.http.Client.request", side_effect=AssertionError("network in tests"))):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        # refresh-universe / crawl-descriptions / import-fd refuse without a recorded 'yes' (CliConsentTest)
        from jevscreen import config, consent
        consent.record(config.Config(), consent.GRAY_SOURCES, "yes")

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class CliTest(_CliBase):
    def test_parser_all_subcommands(self) -> None:
        p = cli.build_parser()
        self.assertEqual(p.parse_args(["init"]).command, "init")
        a = p.parse_args(["refresh-universe", "--with-history", "--history-batch", "500"])
        self.assertTrue(a.with_history)
        self.assertEqual(a.history_batch, 500)
        self.assertEqual(p.parse_args(["import-fd", "--fd-duckdb", "/x.duckdb"]).fd_duckdb.name, "x.duckdb")
        a = p.parse_args(["crawl-descriptions", "--limit", "10", "--rate", "0.5", "--all"])
        self.assertEqual((a.limit, a.rate, a.all), (10, 0.5, True))
        self.assertTrue(p.parse_args(["coverage", "--json"]).json)
        self.assertEqual(p.parse_args(["status"]).command, "status")
        # exact per module (a stray or leftover command fails): the base commands, then the ones each add-on
        # module wires in with its own COMMANDS table
        from jevscreen import agent_cli, ondemand_cli, quickstart_cli, why_cli
        extra = {"agent_cli": set(agent_cli.COMMANDS), "quickstart_cli": set(quickstart_cli.COMMANDS),
                 "ondemand_cli": set(ondemand_cli.COMMANDS), "why_cli": set(why_cli.COMMANDS)}
        self.assertEqual(extra, {"agent_cli": {"doctor", "keys", "consent"}, "quickstart_cli": {"quickstart", "page"},
                                 "ondemand_cli": {"fetch-docs"}, "why_cli": {"why"}})
        self.assertEqual(set(cli.COMMANDS) - set().union(*extra.values()),
                         {"init", "refresh-universe", "import-fd", "crawl-descriptions", "sync-sec", "sync-cninfo",
                          "sync-edinet", "sync-dart", "keywords", "coverage", "status", "screen", "cards", "answer",
                          "sieve", "pack", "sync-mops", "sync-bse"})
        self.assertLessEqual(set().union(*extra.values()), set(cli.COMMANDS))
        a = p.parse_args(["sync-sec"])
        self.assertEqual((a.limit, a.refresh, a.forms, a.after_block), (None, False, ("10-K", "20-F"), False))
        a = p.parse_args(["sync-sec", "--limit", "5", "--refresh", "--forms", "10-k, 20-F,10-K", "--after-block"])
        self.assertEqual((a.limit, a.refresh, a.forms, a.after_block), (5, True, ("10-K", "20-F"), True))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            p.parse_args(["sync-sec", "--forms", " , "])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            p.parse_args([])

    def test_blocked_exit_code_2(self) -> None:
        def refresh_universe(cfg, con, client, **kw):
            raise Blocked("https://scanner.tradingview.com/global/scan", 429, "http_429")
        with mock.patch.dict(sys.modules, {cli.SCANNER: _fake_module(cli.SCANNER, refresh_universe=refresh_universe)}):
            code, out, _ = self.run_cli("refresh-universe")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["status"], "blocked")
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["last_runs"][0]["status"], "blocked")

    def test_cooldown_after_block(self) -> None:
        calls = []
        def refresh_universe(cfg, con, client, **kw):
            calls.append(kw)
            if len(calls) == 1:
                raise Blocked("https://scanner.tradingview.com/global/scan", 429, "http_429")
            return {"rows": 1}
        crawl = lambda cfg, client, **kw: {"ok": 0}  # noqa: E731
        fakes = {cli.SCANNER: _fake_module(cli.SCANNER, refresh_universe=refresh_universe),
                 cli.PROFILES: _fake_module(cli.PROFILES, crawl=crawl)}
        with mock.patch.dict(sys.modules, fakes):
            self.assertEqual(self.run_cli("refresh-universe")[0], 2)
            code, out, err = self.run_cli("refresh-universe")          # refused before any adapter call
            self.assertEqual((code, json.loads(out)["status"], len(calls)), (2, "cooldown", 1))
            self.assertIn("--after-block", err)
            self.assertEqual(self.run_cli("crawl-descriptions")[0], 0)  # other source unaffected
            self.assertEqual(self.run_cli("refresh-universe", "--after-block")[0], 0)
            self.assertEqual(len(calls), 2)
            self.assertEqual(self.run_cli("refresh-universe")[0], 0)     # last run ok again: no cooldown

    def test_old_block_does_not_cool_down(self) -> None:
        from jevscreen import config
        con = store.connect(config.load())
        old = store.now_utc() - dt.timedelta(hours=cli.COOLDOWN_HOURS + 1)
        con.execute("INSERT INTO runs VALUES ('r0', 'crawl tradingview_profile', ?, ?, 'blocked', 1, NULL)", [old, old])
        self.assertIsNone(cli.recent_block(con, "crawl-descriptions"))
        con.execute("UPDATE runs SET started_at = ? WHERE run_id = 'r0'", [store.now_utc()])
        self.assertIsNotNone(cli.recent_block(con, "crawl-descriptions"))
        con.close()

    def test_blocked_summary_exit_code_2(self) -> None:
        crawl = lambda cfg, client, **kw: {"stopped": "blocked:http_403", "ok": 3}  # noqa: E731
        with mock.patch.dict(sys.modules, {cli.PROFILES: _fake_module(cli.PROFILES, crawl=crawl)}):
            self.assertEqual(self.run_cli("crawl-descriptions")[0], 2)

    def test_success_passes_arguments(self) -> None:
        seen = {}
        def refresh_universe(cfg, con, client, **kw):
            seen.update(kw)
            return {"rows": 1}
        with mock.patch.dict(sys.modules, {cli.SCANNER: _fake_module(cli.SCANNER, refresh_universe=refresh_universe)}):
            code, out, _ = self.run_cli("refresh-universe", "--with-history", "--history-batch", "7")
        self.assertEqual(code, 0)
        self.assertEqual(seen, {"with_history": True, "history_batch": 7})
        self.assertEqual(json.loads(out)["result"], {"rows": 1})

    def test_no_duplicate_run_when_adapter_journals(self) -> None:
        def import_descriptions(cfg, con, **kw):
            con.execute("INSERT INTO runs VALUES ('r1', 'import-fd', now(), now(), 'ok', 0, NULL)")
            return {"imported": 0}
        with mock.patch.dict(sys.modules, {cli.FD_LOCAL: _fake_module(cli.FD_LOCAL,
                                                                      import_descriptions=import_descriptions)}):
            self.assertEqual(self.run_cli("import-fd")[0], 0)
        runs = json.loads(self.run_cli("status")[1])["last_runs"]
        self.assertEqual(len(runs), 1)

    def test_rate_clamp_warning(self) -> None:
        seen = {}
        def crawl(cfg, client, **kw):
            assert callable(kw.pop("on_blocked"))   # the CLI hears about a block before any database wait
            seen.update(kw, interval=client.min_interval_s)
            return {"ok": 0}
        with mock.patch.dict(sys.modules, {cli.PROFILES: _fake_module(cli.PROFILES, crawl=crawl)}):
            code, _, err = self.run_cli("crawl-descriptions", "--rate", "5", "--limit", "3")
            self.assertEqual(code, 0)
            self.assertIn("clamped", err)
            self.assertEqual(seen, {"limit": 3, "only_missing": True, "workers": 3, "after_block": False, "interval": 1 / 4.4})
            code, _, err = self.run_cli("crawl-descriptions", "--rate", "0.5", "--all")
            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertEqual(seen, {"limit": None, "only_missing": False, "workers": 3, "after_block": False, "interval": 2.0})
        self.assertEqual(self.run_cli("crawl-descriptions", "--rate", "0")[0], 1)

    def test_missing_adapter_is_error_not_crash(self) -> None:
        with mock.patch.dict(sys.modules, {cli.FD_LOCAL: None}):
            code, _, err = self.run_cli("import-fd")
        self.assertEqual(code, 1)
        self.assertIn("import-fd", err)

    def test_init_coverage_status(self) -> None:
        code, out, _ = self.run_cli("init")
        self.assertEqual(code, 0)
        self.assertIn("universe", json.loads(out)["tables"])
        code, out, _ = self.run_cli("coverage", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["totals"]["companies"], 0)
        code, out, _ = self.run_cli("coverage")
        self.assertIn("TOTAL", out)
        code, out, _ = self.run_cli("status")
        self.assertEqual(json.loads(out)["rows"]["securities"], 0)


SEC_UA = "Test Person test.person@example.org"
HOLD_LOCK = "import duckdb, sys\ncon = duckdb.connect(sys.argv[1])\nprint('ready', flush=True)\nsys.stdin.read()\n"
PROBE_WRITE = ("import sys\nsys.path.insert(0, sys.argv[1])\nfrom pathlib import Path\nfrom jevscreen import store\n"
               "from jevscreen.config import Config\ntry:\n"
               "    with store.session(Config(home=Path(sys.argv[2])), wait_s=5) as con:\n"
               "        con.execute(\"INSERT INTO runs VALUES ('probe', 'probe', now()::TIMESTAMP, NULL, 'ok', 0, NULL)\")\n"
               "except store.StoreLocked:\n    sys.exit(3)\n")
SRC = str(Path(__file__).resolve().parents[1] / "src")


class CliSessionsAndSecTest(_CliBase):
    """Short-session behaviour of the CLI, the sync-sec command and StoreLocked reporting."""

    def hold_lock(self) -> subprocess.Popen:
        """Another process holding the DuckDB write lock until the test ends."""
        proc = subprocess.Popen([sys.executable, "-c", HOLD_LOCK, str(Path(self.tmp.name) / "jevscreen.duckdb")],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (proc.stdin.close(), proc.wait(timeout=30), proc.stdout.close()))
        self.assertEqual(proc.stdout.readline().strip(), "ready")
        return proc

    def sec_module(self, sync) -> dict:
        return {cli.SEC_EDGAR: _fake_module(cli.SEC_EDGAR, sync=sync)}

    def runs(self) -> list[tuple]:
        with store.session(self._cfg(), read_only=True, wait_s=5) as con:
            return con.execute("SELECT command, status, note FROM runs ORDER BY started_at").fetchall()

    def _cfg(self):
        from jevscreen import config
        return config.load()

    def test_read_only_commands_report_store_locked(self) -> None:
        self.assertEqual(self.run_cli("init")[0], 0)
        self.hold_lock()
        with mock.patch.object(cli, "READ_WAIT_S", 0.3):
            for command in ("status", "coverage"):
                code, out, err = self.run_cli(command)
                self.assertEqual(code, cli.EXIT_LOCKED, command)
                self.assertEqual(json.loads(out)["status"], "locked")
                self.assertIn("locked by another process", err)
                self.assertNotIn("Traceback", err)

    def test_write_commands_report_store_locked(self) -> None:
        self.assertEqual(self.run_cli("init")[0], 0)
        self.hold_lock()
        called = []
        fake = _fake_module(cli.FD_LOCAL, import_descriptions=lambda cfg, con, **kw: called.append(1))
        with mock.patch.object(cli, "WRITE_WAIT_S", 0.3), mock.patch.dict(sys.modules, {cli.FD_LOCAL: fake}):
            code, out, err = self.run_cli("import-fd")
        self.assertEqual((code, json.loads(out)["status"], called), (cli.EXIT_LOCKED, "locked", []))

    def test_read_only_on_store_without_new_tables(self) -> None:
        con = store.connect(self._cfg())
        con.execute("DROP TABLE documents")
        con.execute("DROP TABLE identifiers")
        con.close()
        code, out, _ = self.run_cli("coverage", "--json")
        self.assertEqual((code, json.loads(out)["totals"]["sec_document"]), (0, 0))
        code, out, _ = self.run_cli("status")
        d = json.loads(out)
        self.assertEqual((code, d["documents"], d["identifiers"]), (0, None, None))

    def test_status_documents_and_identifiers(self) -> None:
        con = store.connect(self._cfg())
        store.upsert_many(con, "documents", ["doc_id", "company_key", "source_id", "form", "text_path"], [
            ["d1", "isin:A", "sec_filing_text", "10-K", "/x/1.txt"], ["d2", "isin:B", "sec_filing_text", "20-F", None]])
        store.upsert_many(con, "identifiers", ["security_id", "id_type", "id_value"],
                          [["NASDAQ:AAPL", "cik", "320193"], ["NYSE:BABA", "cik", "1577552"]])
        con.close()
        d = json.loads(self.run_cli("status")[1])
        self.assertEqual(d["documents"], {"rows": 2, "with_text": 1, "companies": 1,
                                          "by_source_form": {"sec_filing_text/10-K": 1, "sec_filing_text/20-F": 1},
                                          "companies_with_text_by_source": {"sec_filing_text": 1}})
        self.assertEqual(d["identifiers"], {"rows": 2, "by_type": {"cik": 2}})
        self.assertEqual((d["rows"]["documents"], d["rows"]["identifiers"]), (2, 2))

    def test_crawl_descriptions_holds_no_connection(self) -> None:
        self.assertEqual(self.run_cli("init")[0], 0)
        probe = []
        def crawl(cfg, client, **kw):
            probe.append(subprocess.run([sys.executable, "-c", PROBE_WRITE, SRC, self.tmp.name],
                                        capture_output=True, timeout=120).returncode)
            return {"ok": 0}
        with mock.patch.dict(sys.modules, {cli.PROFILES: _fake_module(cli.PROFILES, crawl=crawl)}):
            self.assertEqual(self.run_cli("crawl-descriptions")[0], 0)
        self.assertEqual(probe, [0])
        self.assertEqual(sorted(r[:2] for r in self.runs()), [("crawl-descriptions", "ok"), ("probe", "ok")])

    def test_sync_sec_passes_arguments_and_redacts_user_agent(self) -> None:
        seen = {}
        def sync(cfg, client, *, limit, forms, refresh):
            seen.update(limit=limit, forms=forms, refresh=refresh, ua=client.user_agent,
                        interval=client.min_interval_s)
            return {"mapped": 2, "echo": f"request made with {client.user_agent}"}
        with mock.patch.dict(os.environ, {"JEVSCREEN_SEC_USER_AGENT": SEC_UA}), \
                mock.patch.dict(sys.modules, self.sec_module(sync)):
            code, out, err = self.run_cli("sync-sec", "--limit", "3", "--forms", "10-K", "--refresh")
        self.assertEqual(code, 0)
        self.assertEqual(seen, {"limit": 3, "forms": ("10-K",), "refresh": True, "ua": SEC_UA, "interval": 0.15})
        self.assertNotIn(SEC_UA, out + err)
        self.assertIn("<sec-user-agent>", json.loads(out)["result"]["echo"])
        runs = self.runs()
        self.assertEqual([r[:2] for r in runs], [("sync-sec", "ok")])
        self.assertNotIn(SEC_UA, runs[0][2])

    def test_sync_sec_error_is_redacted(self) -> None:
        def sync(cfg, client, **kw):
            raise RuntimeError(f"bad header {client.user_agent}")
        with mock.patch.dict(os.environ, {"JEVSCREEN_SEC_USER_AGENT": SEC_UA}), \
                mock.patch.dict(sys.modules, self.sec_module(sync)):
            code, out, err = self.run_cli("sync-sec")
        self.assertEqual(code, 1)
        self.assertNotIn(SEC_UA, out + err)
        self.assertIn("<sec-user-agent>", err)
        self.assertEqual(self.runs()[0][:2], ("sync-sec", "error"))
        self.assertNotIn(SEC_UA, self.runs()[0][2])

    def test_sync_sec_user_agent_from_file(self) -> None:
        (Path(self.tmp.name) / "sec_user_agent").write_text(SEC_UA + "\n")
        seen = []
        env = {k: v for k, v in os.environ.items() if k != "JEVSCREEN_SEC_USER_AGENT"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.dict(sys.modules, self.sec_module(lambda cfg, client, **kw: seen.append(client.user_agent))):
            self.assertEqual(self.run_cli("sync-sec")[0], 0)
        self.assertEqual(seen, [SEC_UA])

    def test_sync_sec_without_user_agent_refuses(self) -> None:
        called = []
        env = {k: v for k, v in os.environ.items() if k != "JEVSCREEN_SEC_USER_AGENT"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.dict(sys.modules, self.sec_module(lambda cfg, client, **kw: called.append(1))):
            code, _, err = self.run_cli("sync-sec")
        self.assertEqual((code, called), (1, []))
        self.assertIn("sec_user_agent", err)
        self.assertIn("JEVSCREEN_SEC_USER_AGENT", err)

    def test_sync_sec_blocked_exit_2_and_cooldown(self) -> None:
        calls = []
        def sync(cfg, client, **kw):
            calls.append(kw)
            if len(calls) == 1:
                raise Blocked("https://www.sec.gov/cgi-bin/browse-edgar", 403, "http_403")
            return {"mapped": 1}
        crawl = lambda cfg, client, **kw: {"ok": 0}  # noqa: E731
        with mock.patch.dict(os.environ, {"JEVSCREEN_SEC_USER_AGENT": SEC_UA}), \
                mock.patch.dict(sys.modules, {**self.sec_module(sync),
                                              cli.PROFILES: _fake_module(cli.PROFILES, crawl=crawl)}):
            code, out, _ = self.run_cli("sync-sec")
            self.assertEqual((code, json.loads(out)["status"]), (2, "blocked"))
            code, out, err = self.run_cli("sync-sec")
            self.assertEqual((code, json.loads(out)["status"], len(calls)), (2, "cooldown", 1))
            self.assertIn("--after-block", err)
            self.assertEqual(self.run_cli("crawl-descriptions")[0], 0)   # other command unaffected
            self.assertEqual(self.run_cli("sync-sec", "--after-block")[0], 0)
            self.assertEqual(self.run_cli("sync-sec")[0], 0)
        self.assertEqual(len(calls), 3)

    def test_sync_sec_blocked_summary_journaled_once_by_adapter_name(self) -> None:
        def sync(cfg, client, **kw):
            with store.session(cfg) as con:
                con.execute("INSERT INTO runs VALUES ('s1', 'sync sec_edgar', ?, ?, 'blocked', 1, NULL)",
                            [store.now_utc(), store.now_utc()])
            return {"status": "blocked", "reason": "http_429"}
        with mock.patch.dict(os.environ, {"JEVSCREEN_SEC_USER_AGENT": SEC_UA}), \
                mock.patch.dict(sys.modules, self.sec_module(sync)):
            self.assertEqual(self.run_cli("sync-sec")[0], 2)
            self.assertEqual([r[:2] for r in self.runs()], [("sync sec_edgar", "blocked")])
            self.assertEqual(json.loads(self.run_cli("sync-sec")[1])["status"], "cooldown")

    def test_missing_sec_adapter_is_error(self) -> None:
        with mock.patch.dict(os.environ, {"JEVSCREEN_SEC_USER_AGENT": SEC_UA}), \
                mock.patch.dict(sys.modules, {cli.SEC_EDGAR: None}):
            code, _, err = self.run_cli("sync-sec")
        self.assertEqual(code, 1)
        self.assertIn("sync-sec", err)



class CliConsentTest(_CliBase):
    """Gray-private commands need a recorded 'yes' for gray-sources before any request or file read."""

    GATED = (("refresh-universe", "tradingview_scanner", cli.SCANNER, "refresh_universe"),
             ("crawl-descriptions", "tradingview_profile", cli.PROFILES, "crawl"),
             ("import-fd", "financedatabase_local", cli.FD_LOCAL, "import_descriptions"))

    def fakes(self, calls):
        mods = {mod: _fake_module(mod, **{fn: (lambda *a, _c=cmd, **k: calls.append(_c) or {"ok": 0})})
                for cmd, _sid, mod, fn in self.GATED}
        return mock.patch.dict(sys.modules, mods)

    def test_refused_without_yes(self) -> None:
        from jevscreen import config, consent
        path = consent.consent_path(config.Config())
        for state in ("unset", "no", "unreadable"):
            if state == "unset":
                path.unlink()
            elif state == "no":
                consent.record(config.Config(), consent.GRAY_SOURCES, "no")
            else:
                path.write_text("{not json", encoding="utf-8")
            for cmd, sid, _mod, _fn in self.GATED:
                calls: list = []
                with self.subTest(state=state, cmd=cmd), self.fakes(calls):
                    code, out, err = self.run_cli(cmd)
                    self.assertEqual(code, cli.EXIT_ERROR)
                    self.assertEqual(json.loads(out), {
                        "command": cmd, "status": "consent_required", "source_id": sid, "consent": state,
                        "hint": "ask the human (AGENTS.md step 5), then record the answer: "
                                "jevscreen consent set gray-sources yes|no"})
                    self.assertIn("consent set gray-sources", err)
                    self.assertEqual(calls, [])                    # the adapter was never reached

    def test_runs_with_yes(self) -> None:
        calls: list = []
        with self.fakes(calls):
            for cmd, _sid, _mod, _fn in self.GATED:
                self.assertEqual(self.run_cli(cmd)[0], cli.EXIT_OK, cmd)
        self.assertEqual(calls, [cmd for cmd, *_ in self.GATED])


class CliGuardsTest(_CliBase):
    """Rate-budget lock, on-disk cooldown marker, result-status exit codes and closing 'running' rows."""

    def _cfg(self):
        from jevscreen import config
        return config.load()

    def sec(self, sync):
        return mock.patch.dict(sys.modules, {cli.SEC_EDGAR: _fake_module(cli.SEC_EDGAR, sync=sync)})

    def ua(self):
        return mock.patch.dict(os.environ, {"JEVSCREEN_SEC_USER_AGENT": SEC_UA})

    def runs(self) -> list[tuple]:
        with store.session(self._cfg(), read_only=True, wait_s=5) as con:
            return con.execute("SELECT command, status, finished_at FROM runs ORDER BY started_at").fetchall()

    def test_second_copy_refused_while_budget_locked(self) -> None:
        from jevscreen import guard
        called = []
        with self.ua(), self.sec(lambda cfg, client, **kw: called.append(1)), \
                guard.budget_lock(self._cfg(), "sec.gov"):
            code, out, err = self.run_cli("sync-sec")
        self.assertEqual((code, json.loads(out)["status"], called), (cli.EXIT_BUSY, "busy", []))
        self.assertIn("sec.gov", err)
        crawl = lambda cfg, client, **kw: {"ok": 0}  # noqa: E731
        with mock.patch.dict(sys.modules, {cli.PROFILES: _fake_module(cli.PROFILES, crawl=crawl)}), \
                guard.budget_lock(self._cfg(), "sec.gov"):
            self.assertEqual(self.run_cli("crawl-descriptions")[0], 0)      # other budget unaffected
        with self.ua(), self.sec(lambda cfg, client, **kw: called.append(1)):
            self.assertEqual(self.run_cli("sync-sec")[0], 0)                # released after the run
        self.assertEqual(called, [1])

    def test_budget_lock_is_exclusive_across_processes(self) -> None:
        from jevscreen import guard
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path\n"
                "from jevscreen import guard; from jevscreen.config import Config\n"
                "try:\n    with guard.budget_lock(Config(home=Path(sys.argv[2])), 'sec.gov'): pass\n"
                "except guard.Busy:\n    sys.exit(4)\n")
        with guard.budget_lock(self._cfg(), "sec.gov"):
            held = subprocess.run([sys.executable, "-c", code, SRC, self.tmp.name], timeout=60).returncode
        free = subprocess.run([sys.executable, "-c", code, SRC, self.tmp.name], timeout=60).returncode
        self.assertEqual((held, free), (4, 0))

    def test_marker_alone_enforces_cooldown_and_ok_run_clears_it(self) -> None:
        from jevscreen import guard
        calls = []
        guard.mark_blocked(self._cfg(), "sync-sec", url="https://data.sec.gov/x", status=429, reason="http_429")
        with self.ua(), self.sec(lambda cfg, client, **kw: calls.append(1) or {"status": "ok"}):
            code, out, _ = self.run_cli("sync-sec")
            self.assertEqual((code, json.loads(out)["status"], calls), (2, "cooldown", []))
            self.assertIn("http_429", json.loads(out)["note"])
            self.assertEqual(self.run_cli("sync-sec", "--after-block")[0], 0)
            self.assertIsNone(guard.recent_block_marker(self._cfg(), "sync-sec"))
            self.assertEqual(self.run_cli("sync-sec")[0], 0)
        self.assertEqual(len(calls), 2)

    def test_blocked_exception_writes_marker(self) -> None:
        from jevscreen import guard
        def sync(cfg, client, **kw):
            raise Blocked("https://data.sec.gov/submissions/CIK1.json", 403, "http_403")
        with self.ua(), self.sec(sync):
            self.assertEqual(self.run_cli("sync-sec")[0], 2)
        self.assertIn("http_403", guard.recent_block_marker(self._cfg(), "sync-sec")["note"])

    def test_result_status_maps_to_exit_code(self) -> None:
        for status, code in (("interrupted", 130), ("stopped_errors", 1), ("store_locked", 1), ("error", 1),
                             ("ok", 0), ("weird", 0)):
            with self.ua(), self.sec(lambda cfg, client, **kw: {"status": status}):
                got, out, _ = self.run_cli("sync-sec")
            self.assertEqual((got, json.loads(out)["status"]), (code, status if status != "weird" else "ok"), status)

    def test_running_adapter_row_closed_on_interrupt(self) -> None:
        def sync(cfg, client, **kw):
            with store.session(cfg) as con:
                con.execute("INSERT INTO runs VALUES ('s1', 'sync sec_edgar', ?, NULL, 'running', 0, NULL)",
                            [store.now_utc()])
            raise KeyboardInterrupt
        with self.ua(), self.sec(sync), self.assertRaises(KeyboardInterrupt):
            self.run_cli("sync-sec")
        rows = self.runs()
        self.assertEqual([r[:2] for r in rows], [("sync sec_edgar", "interrupted")])
        self.assertIsNotNone(rows[0][2])

    def test_non_ascii_user_agent_redacted_in_journal(self) -> None:
        ua = "Jörg Müller jorg@example.org"
        with mock.patch.dict(os.environ, {"JEVSCREEN_SEC_USER_AGENT": ua}), \
                self.sec(lambda cfg, client, **kw: {"echo": client.user_agent}):
            code, out, err = self.run_cli("sync-sec")
        self.assertEqual(code, 0)
        self.assertNotIn("jorg@example.org", out + err)
        with store.session(self._cfg(), read_only=True, wait_s=5) as con:
            (note,) = con.execute("SELECT note FROM runs").fetchone()
        self.assertNotIn("jorg@example.org", note)
        self.assertIn("<sec-user-agent>", note)


EDINET_KEY = "edinet-test-key-0123456789abcdef"
DART_KEY = "dart0123456789abcdef0123456789abcdef0123"


class CliOfficialSourcesTest(_CliBase):
    """sync-cninfo / sync-edinet / sync-dart and keywords: parsing, key handling, redaction, exit codes."""

    def _cfg(self):
        from jevscreen import config
        return config.load()

    def mod(self, name, sync):
        return mock.patch.dict(sys.modules, {name: _fake_module(name, sync=sync)})

    def env(self, **kw):
        base = {k: v for k, v in os.environ.items()
                if k not in ("JEVSCREEN_EDINET_API_KEY", "JEVSCREEN_OPENDART_API_KEY")}
        return mock.patch.dict(os.environ, {**base, **kw}, clear=True)

    def runs(self) -> list[tuple]:
        with store.session(self._cfg(), read_only=True, wait_s=5) as con:
            return con.execute("SELECT command, status, note FROM runs ORDER BY started_at").fetchall()

    def test_parse_new_commands(self) -> None:
        p = cli.build_parser()
        a = p.parse_args(["sync-cninfo"])
        self.assertEqual((a.limit, a.kind, a.min_mcap, a.refresh, a.codes, a.after_block),
                         (None, "summary", None, False, None, False))
        a = p.parse_args(["sync-cninfo", "--limit", "5", "--kind", "full", "--min-mcap", "1e9", "--refresh",
                          "--codes", "300386, 600519", "--after-block"])
        self.assertEqual((a.limit, a.kind, a.min_mcap, a.refresh, a.codes, a.after_block),
                         (5, "full", 1e9, True, ["300386", "600519"], True))
        for command in ("sync-edinet", "sync-dart"):
            a = p.parse_args([command, "--limit", "3", "--min-mcap", "5e8", "--refresh"])
            self.assertEqual((a.limit, a.min_mcap, a.refresh, a.codes), (3, 5e8, True, None))
            self.assertFalse(hasattr(a, "kind"))
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                p.parse_args([command, "--kind", "full"])
        a = p.parse_args(["keywords", "人形机器人", "--refresh"])
        self.assertEqual((a.idea, a.refresh), ("人形机器人", True))
        for bad in (["sync-cninfo", "--kind", "pdf"], ["sync-dart", "--min-mcap", "nan"], ["sync-edinet", "--limit", "0"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                p.parse_args(bad)

    def test_cninfo_passes_arguments_and_journals(self) -> None:
        seen = {}
        def sync(cfg, client, **kw):
            seen.update(kw, interval=client.min_interval_s)
            return {"status": "ok", "documents": 2}
        with self.mod(cli.CNINFO, sync):
            code, out, _ = self.run_cli("sync-cninfo", "--limit", "2", "--kind", "full", "--min-mcap", "1e9",
                                        "--codes", "300386")
        self.assertEqual(code, 0)
        self.assertEqual(seen, {"limit": 2, "codes": ["300386"], "refresh": False, "min_mcap_usd": 1e9,
                                "kind": "full", "interval": 1.0})
        self.assertEqual(json.loads(out)["result"]["documents"], 2)
        self.assertEqual([r[:2] for r in self.runs()], [("sync-cninfo", "ok")])

    def test_missing_key_refused_before_any_request(self) -> None:
        called = []
        for command, module, env, filename in (("sync-edinet", cli.EDINET, "JEVSCREEN_EDINET_API_KEY", "edinet_api_key"),
                                               ("sync-dart", cli.DART, "JEVSCREEN_OPENDART_API_KEY", "opendart_api_key")):
            with self.env(), self.mod(module, lambda cfg, client, **kw: called.append(1)):
                code, out, err = self.run_cli(command)
            self.assertEqual((code, out), (1, ""), command)
            self.assertIn(filename, err)
            self.assertIn(env, err)
        self.assertEqual(called, [])

    def test_key_from_file_and_redaction(self) -> None:
        (Path(self.tmp.name) / "opendart_api_key").write_text(DART_KEY + "\n")
        seen = []
        def sync(cfg, client, **kw):
            seen.append(cfg.opendart_api_key())
            return {"status": "ok", "echo": f"https://opendart.fss.or.kr/api/list.json?crtfc_key={DART_KEY}"}
        with self.env(), self.mod(cli.DART, sync):
            code, out, err = self.run_cli("sync-dart")
        self.assertEqual((code, seen), (0, [DART_KEY]))
        self.assertNotIn(DART_KEY, out + err)
        self.assertIn("<opendart-api-key>", json.loads(out)["result"]["echo"])
        self.assertNotIn(DART_KEY, self.runs()[0][2])

    def test_error_text_is_redacted(self) -> None:
        def sync(cfg, client, **kw):
            raise RuntimeError(f"HTTP 500 for https://api.edinet-fsa.go.jp/api/v2/documents.json?Subscription-Key={EDINET_KEY}")
        with self.env(JEVSCREEN_EDINET_API_KEY=EDINET_KEY), self.mod(cli.EDINET, sync):
            code, out, err = self.run_cli("sync-edinet")
        self.assertEqual(code, 1)
        self.assertNotIn(EDINET_KEY, out + err)
        self.assertIn("<edinet-api-key>", err)
        self.assertEqual(self.runs()[0][:2], ("sync-edinet", "error"))
        self.assertNotIn(EDINET_KEY, self.runs()[0][2])

    def test_blocked_exit_2_cooldown_and_marker_redacted(self) -> None:
        from jevscreen import guard
        calls = []
        def sync(cfg, client, **kw):
            calls.append(kw)
            if len(calls) == 1:
                raise Blocked(f"https://api.edinet-fsa.go.jp/api/v2/documents.json?Subscription-Key={EDINET_KEY}",
                              429, "http_429")
            return {"status": "ok"}
        with self.env(JEVSCREEN_EDINET_API_KEY=EDINET_KEY), self.mod(cli.EDINET, sync), \
                self.mod(cli.CNINFO, lambda cfg, client, **kw: {"status": "ok"}):
            code, out, _ = self.run_cli("sync-edinet")
            self.assertEqual((code, json.loads(out)["status"]), (2, "blocked"))
            self.assertNotIn(EDINET_KEY, out)
            marker = (Path(self.tmp.name) / "cooldown" / "sync-edinet.json").read_text()
            self.assertNotIn(EDINET_KEY, marker)
            self.assertIn("<edinet-api-key>", marker)
            code, out, err = self.run_cli("sync-edinet")
            self.assertEqual((code, json.loads(out)["status"], len(calls)), (2, "cooldown", 1))
            self.assertNotIn(EDINET_KEY, out + err)
            self.assertEqual(self.run_cli("sync-cninfo")[0], 0)            # other command unaffected
            self.assertEqual(self.run_cli("sync-edinet", "--after-block")[0], 0)
            self.assertIsNone(guard.recent_block_marker(self._cfg(), "sync-edinet"))
        self.assertNotIn(EDINET_KEY, json.dumps(self.runs(), default=str))

    def test_budget_lock_busy_and_result_statuses(self) -> None:
        from jevscreen import guard
        called = []
        with self.mod(cli.CNINFO, lambda cfg, client, **kw: called.append(1)), guard.budget_lock(self._cfg(), "cninfo"):
            code, out, _ = self.run_cli("sync-cninfo")
        self.assertEqual((code, json.loads(out)["status"], called), (cli.EXIT_BUSY, "busy", []))
        for status, expect in (("interrupted", 130), ("stopped_errors", 1), ("blocked", 2)):
            with self.mod(cli.CNINFO, lambda cfg, client, **kw: {"status": status}):
                got, _, _ = self.run_cli("sync-cninfo", "--after-block")
            self.assertEqual(got, expect, status)

    def test_missing_adapter_and_interrupt(self) -> None:
        with mock.patch.dict(sys.modules, {cli.CNINFO: None}):
            code, _, err = self.run_cli("sync-cninfo")
        self.assertEqual(code, 1)
        self.assertIn("sync-cninfo", err)
        def sync(cfg, client, **kw):
            raise cli.Terminated("SIGTERM")
        with self.mod(cli.CNINFO, sync):
            code, out, _ = self.run_cli("sync-cninfo")
        self.assertEqual((code, json.loads(out)["status"]), (130, "interrupted"))
        self.assertEqual(self.runs()[-1][:2], ("sync-cninfo", "interrupted"))

    def test_on_blocked_callback_forces_exit_2(self) -> None:
        def sync(cfg, client, *, limit=None, codes=None, kind="summary", refresh=False, min_mcap_usd=None,
                 on_blocked=None):
            on_blocked(Blocked("http://www.cninfo.com.cn/new/hisAnnouncement/query", 403, "http_403"))
            raise store.StoreLocked("final flush failed")
        with self.mod(cli.CNINFO, sync):
            code, out, _ = self.run_cli("sync-cninfo")
        self.assertEqual((code, json.loads(out)["status"], json.loads(out)["reason"]), (2, "blocked", "http_403"))

    def test_keywords_command(self) -> None:
        calls = []
        def generate(cfg, idea, *, refresh=False):
            calls.append((idea, refresh))
            return {"idea_en": "Humanoid robots", "keywords": {"en": ["humanoid robot"], "zh": ["人形机器人"],
                                                                "ja": [], "ko": []}, "model": "fake", "cached": True}
        with mock.patch.dict(sys.modules, {cli.KEYWORDS: _fake_module(cli.KEYWORDS, generate=generate)}):
            code, out, _ = self.run_cli("keywords", "人形机器人", "--refresh")
        self.assertEqual((code, calls), (0, [("人形机器人", True)]))
        d = json.loads(out)
        self.assertEqual((d["status"], d["idea_en"], d["keywords"]["zh"]), ("ok", "Humanoid robots", ["人形机器人"]))
        self.assertIn("人形机器人", out)                                  # printed as UTF-8, not escaped

        class KeywordsUnavailable(RuntimeError):
            pass

        def unavailable(cfg, idea, **kw):
            raise KeywordsUnavailable("Qwen model not in the local HF cache")
        with mock.patch.dict(sys.modules, {cli.KEYWORDS: _fake_module(cli.KEYWORDS, generate=unavailable)}):
            code, out, err = self.run_cli("keywords", "人形机器人")
        self.assertEqual((code, json.loads(out)["status"]), (1, "unavailable"))
        self.assertIn("KeywordsUnavailable", err)

    def test_status_documents_by_source(self) -> None:
        con = store.connect(self._cfg())
        store.upsert_many(con, "documents", ["doc_id", "company_key", "source_id", "form", "text_path"], [
            ["d1", "isin:A", "sec_filing_text", "10-K", "/x/1.txt"],
            ["d2", "isin:C", "cninfo_annual_report", "年度报告摘要", "/x/2.txt"],
            ["d3", "isin:C", "cninfo_annual_report", "年度报告", "/x/3.txt"],
            ["d4", "isin:J", "edinet_yuho", "有価証券報告書", None]])
        con.close()
        d = json.loads(self.run_cli("status")[1])["documents"]
        self.assertEqual(d["companies_with_text_by_source"], {"cninfo_annual_report": 1, "sec_filing_text": 1})
        self.assertEqual(d["by_source_form"]["edinet_yuho/有価証券報告書"], 1)


if __name__ == "__main__":
    unittest.main()
