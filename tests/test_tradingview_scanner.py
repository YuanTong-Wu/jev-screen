"""Offline tests for the TradingView screener adapter. No network: the Client is a fake serving fixture payloads.

The scan fixtures are SYNTHETIC (invented companies, ISINs and figures; see tools/synthetic_fixtures/tradingview.py).
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

import duckdb  # noqa: E402

from jevscreen import store  # noqa: E402
from jevscreen.config import Config  # noqa: E402
from jevscreen.http import Blocked, Response  # noqa: E402
from jevscreen.sources import tradingview_scanner as tv  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
FIX_COLUMNS: list[str] = json.loads((FIX / "tv_columns.json").read_text())
AS_OF = dt.date(2026, 9, 26)


def _load(name: str) -> list[dict]:
    rows = json.loads((FIX / name).read_text())["data"]
    return [{"s": r["s"], **dict(zip(FIX_COLUMNS, r["d"]))} for r in rows]


USD_ROWS = _load("tv_scan_usd.json")
LOCAL_ROWS = _load("tv_scan_local.json")
USD = {r["s"]: r for r in USD_ROWS}
LOCAL = {r["s"]: r for r in LOCAL_ROWS}


class FakeClient:
    """Answers scanner POSTs like TradingView: evaluates 'equal' filters or explicit tickers against the fixtures,
    returns only requested columns (unknown columns -> null, as the real endpoint does)."""

    def __init__(self, exclude: set[str] = frozenset(), block_on=None):
        self.exclude, self.block_on, self.calls = set(exclude), block_on, []

    def post_json(self, url: str, body: dict, **kw) -> Response:
        assert url == tv.SCAN_URL
        self.calls.append(body)
        if self.block_on and self.block_on(body):
            raise Blocked(url, 429, "http_429")
        local = body.get("price_conversion") == {"to_symbol": False}
        rows = [r for r in (LOCAL_ROWS if local else USD_ROWS) if r["s"] not in self.exclude]
        if "symbols" in body:
            wanted = set(body["symbols"]["tickers"])
            rows = [r for r in rows if r["s"] in wanted]
        else:
            rows = [r for r in rows if all(f["operation"] == "equal" and r.get(f["left"]) == f["right"]
                                           for f in body["filter"])]
        data = [{"s": r["s"], "d": [r.get(c) for c in body["columns"]]} for r in rows]
        raw = json.dumps({"totalCount": len(data), "data": data}).encode()
        return Response(url, 200, raw, {}, 0.0)


def is_dr_request(body: dict) -> bool:
    return any(f.get("left") == "type" and f.get("right") == "dr" for f in body.get("filter", []))


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name)).ensure()
        self.con = duckdb.connect(":memory:")
        store.init(self.con)

    def tearDown(self) -> None:
        self.con.close()
        self.tmp.cleanup()

    def refresh(self, client=None, **kw) -> dict:
        return tv.refresh_universe(self.cfg, self.con, client or FakeClient(), as_of=AS_OF, **kw)

    def one(self, sql: str, params=()):
        return self.con.execute(sql, list(params)).fetchone()


class SyntheticFixtureCodes(unittest.TestCase):
    def test_a_share_codes_are_in_the_unlisted_block(self):
        # invented A-share issuers use the unlisted 609xxx (SSE) block, never a live code like 609999
        codes = sorted(s for s in USD if s.startswith(("SSE:", "SZSE:")))
        self.assertTrue(codes)
        self.assertEqual([c for c in codes if not c.split(":")[1].startswith("609")], [])


class PureHelpers(unittest.TestCase):
    def test_body_default_filter_mode(self):
        b = tv.build_scan_body(tv.UNIVERSE_FILTERS["stock"], ["name"], local=False)
        self.assertNotIn("price_conversion", b)
        self.assertEqual(b["range"], [0, tv.SCAN_RANGE])
        self.assertEqual(b["sort"], {"sortBy": "name", "sortOrder": "asc"})
        self.assertNotIn("symbols", b)

    def test_body_local_tickers(self):
        b = tv.build_scan_body(None, ["name"], local=True, tickers=["TSE:9901"])
        self.assertEqual(b["price_conversion"], {"to_symbol": False})
        self.assertEqual(b["symbols"], {"tickers": ["TSE:9901"]})
        self.assertNotIn("filter", b)
        self.assertNotIn("range", b)

    def test_universe_filters(self):
        self.assertEqual({(f["left"], f["right"]) for f in tv.UNIVERSE_FILTERS["stock"]},
                         {("type", "stock"), ("is_primary", True), ("subtype", "common")})
        self.assertEqual({(f["left"], f["right"]) for f in tv.UNIVERSE_FILTERS["dr"]},
                         {("type", "dr"), ("is_primary", True)})

    def test_history_columns(self):
        self.assertEqual(len(tv.HISTORY_COLUMNS), 8)
        self.assertTrue(set(tv.CURRENT_COLUMNS + [tv.HISTORY_PERIOD_COLUMN] + tv.HISTORY_COLUMNS) <= set(FIX_COLUMNS))

    def test_parse_rows_fixture(self):
        payload = json.loads((FIX / "tv_scan_usd.json").read_text())
        rows = tv.parse_rows(payload, FIX_COLUMNS)
        self.assertEqual(len(rows), 20)
        self.assertEqual(rows[0]["s"], "TSE:9901")
        self.assertEqual(rows[0]["description"], "Okuzan Motor Corp.")

    def test_parse_rows_length_mismatch(self):
        with self.assertRaises(ValueError):
            tv.parse_rows({"data": [{"s": "X:Y", "d": [1, 2]}]}, ["a", "b", "c"])

    def test_parse_rows_malformed(self):
        for bad in ({}, {"data": None}, {"data": [{"d": [1]}]}, {"data": [{"s": "X:Y", "d": "1"}]}, []):
            with self.assertRaises(ValueError):
                tv.parse_rows(bad, ["a"])

    def test_column_coverage_flags_unknown_column(self):
        rows = [{"s": "A:1", "a": 1, "bogus": None}, {"s": "A:2", "a": None, "bogus": None}]
        self.assertEqual(tv.column_coverage(rows, ["a", "bogus"]), {"a": 1, "bogus": 0})

    def test_fx(self):
        self.assertAlmostEqual(tv.fx_local_to_usd(USD["LSE:NRTH"], LOCAL["LSE:NRTH"]), 1.321, places=3)
        self.assertAlmostEqual(tv.fx_local_to_usd(USD["TSE:9901"], LOCAL["TSE:9901"]), 0.0063, places=4)
        self.assertEqual(tv.fx_local_to_usd(USD["NASDAQ:ORCD"], LOCAL["NASDAQ:ORCD"]), 1.0)

    def test_fx_missing_or_nonpositive(self):
        self.assertIsNone(tv.fx_local_to_usd({"market_cap_basic": None}, {"market_cap_basic": 5}))
        self.assertIsNone(tv.fx_local_to_usd({"market_cap_basic": 5}, {"market_cap_basic": 0}))
        self.assertIsNone(tv.fx_local_to_usd({"market_cap_basic": -1}, {"market_cap_basic": 5}))
        self.assertIsNone(tv.fx_local_to_usd(None, LOCAL["TSE:9901"]))

    def test_epoch_to_date(self):
        self.assertEqual(tv.epoch_to_date(1774915200), dt.date(2026, 3, 31))
        self.assertIsNone(tv.epoch_to_date(None))
        self.assertIsNone(tv.epoch_to_date("x"))
        self.assertIsNone(tv.epoch_to_date(1e20))

    def test_zip_history_basic(self):
        self.assertEqual(tv.zip_history([2025, 2024], [10, 9]), [(2025, 10.0), (2024, 9.0)])

    def test_zip_history_length_mismatch_uses_prefix(self):
        self.assertEqual(tv.zip_history([2025, 2024, 2023], [10, 9]), [(2025, 10.0), (2024, 9.0)])
        self.assertEqual(tv.zip_history([2025], [10, 9, 8]), [(2025, 10.0)])

    def test_zip_history_null_period_skipped_null_value_kept(self):
        self.assertEqual(tv.zip_history([2025, None, 2023], [None, 9, 8]), [(2025, None), (2023, 8.0)])

    def test_zip_history_missing_arrays(self):
        self.assertEqual(tv.zip_history(None, [1]), [])
        self.assertEqual(tv.zip_history([2025], None), [])
        self.assertEqual(tv.zip_history([], []), [])


class Refresh(Base):
    def test_summary_and_requests(self):
        s = self.refresh()
        self.assertEqual(s["requests"], 4)
        self.assertEqual(s["rows_per_filter"]["stock"], {"usd": 13, "local": 13})
        self.assertEqual(s["rows_per_filter"]["dr"], {"usd": 4, "local": 4})
        self.assertEqual(s["securities_upserted"], 17)
        self.assertEqual(s["market_rows"], 17)
        self.assertEqual(s["deactivated"], 0)
        self.assertEqual(s["warnings"], [])
        self.assertEqual(s["column_coverage"]["universe_usd"]["description"], 17)
        self.assertEqual(self.one("SELECT count(*) FROM snapshots WHERE status='ok' AND raw_sha256 IS NOT NULL")[0], 4)
        self.assertEqual(self.one("SELECT status, requests FROM runs"), ("ok", 4))
        for (path,) in self.con.execute("SELECT raw_path FROM snapshots").fetchall():
            self.assertTrue(Path(path).exists())

    def test_non_primary_and_preferred_excluded(self):
        self.refresh()
        ids = {r[0] for r in self.con.execute("SELECT security_id FROM securities").fetchall()}
        self.assertNotIn("BMFBOVESPA:MARE4", ids)
        self.assertNotIn("OTC:HBDGY", ids)
        self.assertNotIn("TSX:TILL", ids)

    def test_security_fields_gbx_to_gbp(self):
        self.refresh()
        row = self.one("SELECT exchange, symbol, name, isin, price_currency, fundamental_currency, company_key, active, "
                       "tv_type, is_primary FROM securities WHERE security_id='LSE:NRTH'")
        self.assertEqual(row, ("LSE", "NRTH", USD["LSE:NRTH"]["description"], "GB00BZ9NRT01", "GBX", "GBP",
                               "isin:GB00BZ9NRT01", True, "stock", True))
        self.assertEqual(self.one("SELECT price_currency, fundamental_currency FROM securities "
                                  "WHERE security_id='TASE:GLPH'"), ("ILA", "ILS"))
        self.assertEqual(self.one("SELECT fundamental_currency FROM securities WHERE security_id='JSE:KRU'")[0], "ZAR")
        # default mode says USD for everything; the stored code must come from local mode
        self.assertEqual(self.one("SELECT fundamental_currency FROM securities WHERE security_id='TSE:9901'")[0], "JPY")

    def test_dr_rows_in_universe_view(self):
        self.refresh()
        uni = {r[0] for r in self.con.execute("SELECT security_id FROM universe").fetchall()}
        for s in ("NYSE:BZAR", "NASDAQ:CHPW", "NASDAQ:PDQH", "EURONEXT:VLB", "HKEX:990", "LSE:NRTH"):
            self.assertIn(s, uni)
        self.assertIsNone(self.one("SELECT tv_subtype FROM securities WHERE security_id='NYSE:BZAR'")[0])

    def test_market_daily(self):
        self.refresh()
        r = self.one("SELECT close, price_currency, market_cap_usd, market_cap_local, fx_local_to_usd, "
                     "shares_outstanding, float_shares FROM market_daily WHERE security_id='TSE:9901' AND as_of=?",
                     [AS_OF])
        self.assertEqual(r[0], USD["TSE:9901"]["close"])
        self.assertEqual(r[1], "JPY")
        self.assertEqual(r[2], USD["TSE:9901"]["market_cap_basic"])
        self.assertEqual(r[3], LOCAL["TSE:9901"]["market_cap_basic"])
        self.assertAlmostEqual(r[4], 0.0063, places=4)
        self.assertEqual(r[5], USD["TSE:9901"]["total_shares_outstanding"])
        # missing float stays NULL, never 0
        self.assertIsNone(self.one("SELECT float_shares FROM market_daily WHERE security_id='EURONEXT:VLB'")[0])

    def test_fundamentals_current_never_mixes_usd_local(self):
        self.refresh()
        rows = self.con.execute("SELECT security_id, metric, period, value_usd, value_local, currency_local, "
                                "fiscal_period_end FROM fundamentals_current").fetchall()
        self.assertTrue(rows)
        cols = {(m, p): c for m, p, c in tv.CURRENT_FUNDAMENTALS}
        for s, m, p, v_usd, v_loc, ccy, fy_end in rows:
            col = cols[(m, p)]
            self.assertEqual(v_usd, USD[s][col], (s, col))
            self.assertEqual(v_loc, LOCAL[s][col], (s, col))
            self.assertEqual(ccy, LOCAL[s]["fundamental_currency_code"])
            self.assertEqual(fy_end, tv.epoch_to_date(LOCAL[s]["fiscal_period_end_fy"]) if p == "fy" else None)
        shel = self.one("SELECT value_usd, value_local, currency_local FROM fundamentals_current "
                        "WHERE security_id='LSE:NRTH' AND metric='total_revenue' AND period='fy'")
        self.assertEqual(shel[2], "GBP")
        self.assertGreater(shel[0] / shel[1], 1.3)  # USD vs GBP, both present, separate columns

    def test_fundamentals_row_written_with_nulls(self):
        self.refresh()
        row = self.one("SELECT value_usd, value_local FROM fundamentals_current WHERE security_id='EURONEXT:VLB' "
                       "AND metric='gross_profit' AND period='ttm'")
        self.assertEqual(row, (USD["EURONEXT:VLB"]["gross_profit_ttm"], LOCAL["EURONEXT:VLB"]["gross_profit_ttm"]))
        self.assertEqual(self.one("SELECT count(*) FROM fundamentals_current WHERE security_id='TSE:9901'")[0], 6)
        self.assertEqual(self.one("SELECT count(*) FROM fundamentals_current")[0], 17 * len(tv.CURRENT_FUNDAMENTALS))

    def test_missing_ttm_today_does_not_reuse_yesterday(self):
        from jevscreen import coverage
        tv.refresh_universe(self.cfg, self.con, FakeClient(), as_of=AS_OF - dt.timedelta(days=1))
        before = coverage.report(self.con)["totals"]["ttm_revenue"]
        saved = USD["LSE:NRTH"]["total_revenue_ttm"], LOCAL["LSE:NRTH"]["total_revenue_ttm"]
        try:
            USD["LSE:NRTH"]["total_revenue_ttm"] = LOCAL["LSE:NRTH"]["total_revenue_ttm"] = None
            self.refresh()
        finally:
            USD["LSE:NRTH"]["total_revenue_ttm"], LOCAL["LSE:NRTH"]["total_revenue_ttm"] = saved
        self.assertEqual(self.one("SELECT value_usd, value_local FROM fundamentals_current WHERE "
                                  "security_id='LSE:NRTH' AND metric='total_revenue' AND period='ttm' AND as_of=?",
                                  [AS_OF]), (None, None))
        self.assertEqual(coverage.report(self.con)["totals"]["ttm_revenue"], before - 1)

    def test_snapshots_are_paired(self):
        self.refresh()
        notes = dict(self.con.execute("SELECT snapshot_id, note FROM snapshots").fetchall())
        kinds = dict(self.con.execute("SELECT snapshot_id, kind FROM snapshots").fetchall())
        for snap, note in notes.items():
            other = json.loads(note)["paired_snapshot"]
            self.assertEqual(json.loads(notes[other])["paired_snapshot"], snap)
            self.assertEqual(kinds[snap].rsplit("_", 1)[0], kinds[other].rsplit("_", 1)[0])
        usd_snap = self.one("SELECT snapshot_id FROM market_daily WHERE security_id='LSE:NRTH'")[0]
        self.assertEqual(kinds[json.loads(notes[usd_snap])["paired_snapshot"]], "universe_stock_local")

    def test_unknown_column_warned(self):
        from unittest import mock
        with mock.patch.object(tv, "CURRENT_COLUMNS", tv.CURRENT_COLUMNS + ["no_such_column"]):
            s = self.refresh()
        self.assertEqual(s["column_coverage"]["universe_usd"]["no_such_column"], 0)
        self.assertEqual(s["warnings"], ["universe_usd: zero coverage for no_such_column",
                                         "universe_local: zero coverage for no_such_column"])

    def test_idempotent_second_run(self):
        self.refresh()
        first = self.one("SELECT first_seen_snapshot FROM securities WHERE security_id='TSE:9901'")[0]
        tables = ["securities", "market_daily", "fundamentals_current"]
        before = {t: self.one(f"SELECT count(*) FROM {t}")[0] for t in tables}
        values = self.con.execute("SELECT * EXCLUDE (snapshot_id) FROM fundamentals_current ORDER BY ALL").fetchall()
        s2 = self.refresh()
        self.assertEqual(s2["deactivated"], 0)
        self.assertEqual({t: self.one(f"SELECT count(*) FROM {t}")[0] for t in tables}, before)
        self.assertEqual(self.con.execute("SELECT * EXCLUDE (snapshot_id) FROM fundamentals_current "
                                          "ORDER BY ALL").fetchall(), values)
        r = self.one("SELECT first_seen_snapshot, last_seen_snapshot FROM securities WHERE security_id='TSE:9901'")
        self.assertEqual(r[0], first)
        self.assertNotEqual(r[1], first)
        self.assertEqual(self.one("SELECT count(*) FROM runs WHERE status='ok'")[0], 2)
        # raw files are content-addressed, so the second run adds none
        self.assertEqual(len(list(self.cfg.raw_dir.rglob("*.json"))), 4)


class Deactivation(Base):
    def test_deactivate_after_full_success(self):
        self.refresh()
        s = self.refresh(FakeClient(exclude={"NASDAQ:ZNS"}))  # 12 of 13 stock rows: still >= 90%
        self.assertEqual((s["deactivated"], s["status"]), (1, "ok"))
        self.assertEqual(self.one("SELECT count(*) FROM securities")[0], 17)  # not deleted
        self.assertEqual(self.one("SELECT active FROM securities WHERE security_id='NASDAQ:ZNS'")[0], False)
        self.assertEqual(self.one("SELECT active FROM securities WHERE security_id='TSE:9901'")[0], True)
        uni = {r[0] for r in self.con.execute("SELECT security_id FROM universe").fetchall()}
        self.assertNotIn("NASDAQ:ZNS", uni)
        # comes back when seen again
        self.refresh()
        self.assertEqual(self.one("SELECT active FROM securities WHERE security_id='NASDAQ:ZNS'")[0], True)

    def _assert_partial(self, s: dict) -> None:
        self.assertEqual((s["deactivated"], s["status"]), (0, "partial"))
        self.assertTrue(s["deactivation_skipped"])
        self.assertTrue(any(w.startswith("deactivation skipped") for w in s["warnings"]))
        self.assertEqual(self.one("SELECT count(*) FROM universe WHERE tv_type='dr'")[0], 4)
        self.assertEqual(self.one("SELECT count(*) FROM securities WHERE active")[0], 17)
        self.assertEqual(self.one("SELECT status FROM runs ORDER BY started_at DESC LIMIT 1")[0], "partial")

    def test_empty_filter_result_skips_deactivation(self):
        self.refresh()
        self._assert_partial(self.refresh(FakeClient(exclude={r["s"] for r in USD_ROWS if r["type"] == "dr"})))

    def test_truncated_total_count_skips_deactivation(self):
        class Truncated(FakeClient):
            def post_json(self, url, body, **kw):
                resp = super().post_json(url, body, **kw)
                payload = json.loads(resp.body)
                if not is_dr_request(body) and "filter" in body:
                    payload = {"totalCount": 999, "data": payload["data"][:12]}
                return Response(url, 200, json.dumps(payload).encode(), {}, 0.0)
        self.refresh()
        self._assert_partial(self.refresh(Truncated()))

    def test_row_drop_below_ratio_skips_deactivation(self):
        self.refresh()
        self._assert_partial(self.refresh(FakeClient(exclude={"NASDAQ:CHPW"})))  # 3 of 4 DR rows < 90%

    def test_interrupt_is_journaled_and_writes_nothing_partial(self):
        class Interrupting(FakeClient):
            def post_json(self, url, body, **kw):
                if is_dr_request(body):
                    raise KeyboardInterrupt
                return super().post_json(url, body, **kw)
        with self.assertRaises(KeyboardInterrupt):
            self.refresh(Interrupting())
        self.assertEqual(self.one("SELECT status FROM runs")[0], "interrupted")
        self.assertEqual(self.one("SELECT count(*) FROM securities")[0], 0)

    def test_blocked_leaves_securities_active(self):
        self.refresh()
        client = FakeClient(exclude={"NASDAQ:ZNS"}, block_on=is_dr_request)
        with self.assertRaises(Blocked):
            self.refresh(client)
        self.assertEqual(len(client.calls), 3)  # stock usd, stock local, then blocked on dr usd
        self.assertEqual(self.one("SELECT count(*) FROM securities WHERE active")[0], 17)
        self.assertEqual(self.one("SELECT status, requests FROM runs ORDER BY started_at DESC LIMIT 1"), ("blocked", 3))
        snap = self.one("SELECT kind, raw_path FROM snapshots WHERE status='blocked'")
        self.assertEqual(snap, ("universe_dr_usd", None))

    def test_blocked_on_first_request_writes_nothing(self):
        with self.assertRaises(Blocked):
            self.refresh(FakeClient(block_on=lambda b: True))
        self.assertEqual(self.one("SELECT count(*) FROM securities")[0], 0)
        self.assertEqual(self.one("SELECT status FROM runs")[0], "blocked")

    def test_parse_error_recorded_and_raised(self):
        class Broken(FakeClient):
            def post_json(self, url, body, **kw):
                return Response(url, 200, b'{"data": [{"s": "A:B", "d": [1]}]}', {}, 0.0)
        with self.assertRaises(ValueError):
            self.refresh(Broken())
        self.assertEqual(self.one("SELECT status FROM runs")[0], "error")
        self.assertEqual(self.one("SELECT status FROM snapshots")[0], "error")


class History(Base):
    def test_history_batches(self):
        client = FakeClient()
        s = self.refresh(client, with_history=True, history_batch=5)
        self.assertEqual(s["history_batches"], 4)  # 17 securities / 5
        self.assertEqual(s["requests"], 8)
        hist_calls = [c for c in client.calls if "symbols" in c]
        self.assertTrue(all(c["price_conversion"] == {"to_symbol": False} for c in hist_calls))
        self.assertTrue(all(len(c["symbols"]["tickers"]) <= 5 for c in hist_calls))
        self.assertEqual(hist_calls[0]["columns"], [tv.HISTORY_PERIOD_COLUMN] + tv.HISTORY_COLUMNS)
        self.assertEqual(self.one("SELECT count(*) FROM snapshots WHERE kind='history_local_batch'")[0], 4)
        self.assertIn("coverage", json.loads(self.one("SELECT note FROM snapshots WHERE kind='history_local_batch' "
                                                      "LIMIT 1")[0]))
        self.assertEqual(s["column_coverage"]["history"][tv.HISTORY_PERIOD_COLUMN], 17)

        r = self.one("SELECT value_local, currency_local FROM fundamentals_annual "
                     "WHERE security_id='TSE:9901' AND fiscal_year=2025 AND metric='total_revenue'")
        self.assertEqual(LOCAL["TSE:9901"][tv.HISTORY_PERIOD_COLUMN][0], 2025)
        self.assertEqual(r, (float(LOCAL["TSE:9901"]["total_revenue_fy_h"][0]), "JPY"))
        self.assertEqual(self.one("SELECT DISTINCT currency_local FROM fundamentals_annual "
                                  "WHERE security_id='LSE:NRTH'")[0], "GBP")
        # ABN has no gross_profit history at all -> no rows (not zero rows with 0 values)
        self.assertEqual(self.one("SELECT count(*) FROM fundamentals_annual WHERE security_id='EURONEXT:VLB' "
                                  "AND metric='gross_profit'")[0], 0)
        expected = sum(len(tv.zip_history(LOCAL[sid][tv.HISTORY_PERIOD_COLUMN], LOCAL[sid][f"{m}_fy_h"]))
                       for sid in {r[0] for r in self.con.execute("SELECT security_id FROM securities").fetchall()}
                       for m in tv.HISTORY_METRICS)
        self.assertEqual(s["history_rows"], expected)
        self.assertEqual(self.one("SELECT count(*) FROM fundamentals_annual")[0], expected)

    def test_history_length_mismatch_warned(self):
        class Short(FakeClient):
            def post_json(self, url, body, **kw):
                resp = super().post_json(url, body, **kw)
                if "symbols" not in body:
                    return resp
                payload = json.loads(resp.body)
                for item in payload["data"]:
                    if item["s"] == "TSE:9901":
                        item["d"][1] = item["d"][1][:3]      # total_revenue_fy_h shorter than periods
                        item["d"][0][1] = None               # a null fiscal period
                return Response(url, 200, json.dumps(payload).encode(), {}, 0.0)
        s = self.refresh(Short(), with_history=True)
        self.assertEqual(s["history_length_mismatches"], 1)
        self.assertTrue(any("length-mismatched" in w for w in s["warnings"]))
        years = [r[0] for r in self.con.execute("SELECT fiscal_year FROM fundamentals_annual WHERE "
                                                "security_id='TSE:9901' AND metric='total_revenue' "
                                                "ORDER BY 1 DESC").fetchall()]
        self.assertEqual(years, [2025, 2023])

    def test_blocked_during_history_keeps_universe(self):
        client = FakeClient(block_on=lambda b: "symbols" in b)
        with self.assertRaises(Blocked):
            self.refresh(client, with_history=True)
        self.assertEqual(self.one("SELECT count(*) FROM securities WHERE active")[0], 17)
        self.assertEqual(self.one("SELECT status FROM runs")[0], "blocked")


if __name__ == "__main__":
    unittest.main()
