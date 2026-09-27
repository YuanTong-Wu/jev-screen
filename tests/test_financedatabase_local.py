"""Tests for the FinanceDatabase summary import. No network; the FD DuckDB is a tiny temporary mimic."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import store  # noqa: E402
from jevscreen.config import Config  # noqa: E402
from jevscreen.sources import financedatabase_local as fd  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
LONG = "operates as a company that designs, makes and sells products and services to customers worldwide."

# listing_id, ticker, exchange, isin, name, security_type, is_delisted, summary
FD_ROWS = [
    ("FD:XHKG:0990.HK", "0990.HK", "HKG", None, "Harbour Digital Holding Ltd.", "common_stock", False, "Harbour " + LONG),
    ("FD:XKRX:009990.KS", "009990.KS", "KSC", None, "Hanbit Electronics Co., Ltd.", "common_stock", False,
     "Hanbit " + LONG),
    ("FD:XNSE:MCS.NS", "MCS.NS", "NSE", None, "Meru Consultancy Services Limited", "common_stock", False, "Meru " + LONG),
    ("FD:XLON:NRTH.L", "NRTH.L", "LSE", "GB00BZ9NRT01", "Northsea Energy plc", "common_stock", False, "Northsea " + LONG),
    ("FD:XETR:SWK.DE", "SWK.DE", "GER", None, "Softwerk SE", "common_stock", False, "Softwerk " + LONG),
    # OTC ADR row carrying the HK ordinary ISIN: matches OTC:HBDGY by ticker and HKEX:990 by ISIN -> conflict.
    ("FD:OTCM:HBDGY", "HBDGY", "PNK", "KYG999901001", "Harbour Digital Holding Ltd.", "common_stock", False,
     "Harbour ADR " + LONG),
    # US line with the Australian ISIN: pure cross-venue ISIN match to ASX:RDG.
    ("FD:XNYS:RDG", "RDG", "NYQ", "AU000000RDG5", "Red Gum Resources Limited", "common_stock", False, "Red Gum " + LONG),
    # Same ticker, different company: rejected by the name check.
    ("FD:XNAS:ZNS", "ZNS", "NMS", None, "Zeta Systems Holdings Inc.", "common_stock", False, "Zeta " + LONG),
    ("FD:XNAS:ORCD", "ORCD", "NMS", None, "Orchard Devices Inc.", "common_stock", True, "Orchard " + LONG),       # delisted
    ("FD:XNAS:CHPW", "CHPW", "NMS", None, "Chipwright Holdings plc", "common_stock", False, "Chip designer."),  # short
    ("FD:XNAS:PDQH", "PDQH", "NMS", None, "PDQ Holdings Inc.", "warrant", False, "PDQ " + LONG),       # non-equity
    # FD BZAR carries the HK ordinary ISIN: NYSE:BZAR (ticker) vs HKEX:9910 (ISIN), both in the universe.
    ("FD:XNYS:BZAR", "BZAR", "NYQ", "KYG999910004", "Bazaar Group Holding Limited", "common_stock", False,
     "Bazaar ADR " + LONG),
    ("FD:XHKG:9910.HK", "9910.HK", "HKG", "KYG999910004", "Bazaar Group Holding Limited", "common_stock", False,
     "Bazaar HK " + LONG),
    ("FD:XNYS:BRK-B", "BRK-B", "NYQ", None, "Berkshire Hathaway Inc.", "common_stock", False, "Berkshire " + LONG),
    ("FD:XTAI:2991.TW", "2991.TW", "TAI", None, "Formosa Wafer Manufacturing Company Limited", "common_stock",
     False, "Formosa " + LONG),
    ("FD:XJPX:9901.T", "9901.T", "JPX", None, "Okuzan Motor Corporation", "common_stock", False, "Okuzan " + LONG),
    ("FD:XSHG:609999.SS", "609999.SS", "SHH", None, "Qingshan Spirits Liquor Co., Ltd.", "common_stock", False,
     "Qingshan " + LONG),
    # No exchange code: inferred from the '.JO' suffix.
    ("FD:XJSE:KRU.JO", "KRU.JO", None, None, "Karoo Media Limited", "common_stock", False, "Karoo " + LONG),
]

EXTRA_SECURITIES = [  # (security_id, name, isin, tv_type, tv_subtype)
    ("NYSE:BRK.B", "Berkshire Hathaway Inc. Class B", "US0846707026", "stock", "common"),
    ("HKEX:9910", "Bazaar Group Holding Ltd.", "KYG999910004", "stock", "common"),
]


def make_fd_duckdb(path: Path) -> None:
    con = duckdb.connect(str(path))
    con.execute("""CREATE TABLE fd_listing_attributes (
        listing_id VARCHAR, ticker VARCHAR, ticker_base VARCHAR, exchange VARCHAR, mic VARCHAR, id_isin VARCHAR,
        name VARCHAR, summary VARCHAR, website VARCHAR, country VARCHAR, security_type VARCHAR,
        is_delisted BOOLEAN, observed_at VARCHAR, observed_date DATE)""")
    for lid, ticker, ex, isin, name, typ, delisted, summary in FD_ROWS:
        con.execute("INSERT INTO fd_listing_attributes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            lid, ticker, ticker.split(".")[0], ex, lid.split(":")[1], isin, name, summary, None, None, typ, delisted,
            "2026-09-20T12:00:04+00:00", dt.date(2026, 9, 20)])
    con.close()


def load_fixture_securities(con) -> int:
    """Securities from the synthetic TradingView scan fixture (plus two extra lines) via store helpers."""
    cols = json.loads((FIXTURES / "tv_columns.json").read_text())
    data = json.loads((FIXTURES / "tv_scan_usd.json").read_text())["data"]
    rows = []
    for r in data:
        d = dict(zip(cols, r["d"]))
        exch, sym = r["s"].split(":", 1)
        rows.append((r["s"], exch, sym, d["description"], d["isin"], d["country"], d["type"], d["subtype"],
                     d["is_primary"], store.company_key(d["isin"], r["s"]), True))
    for sid, name, isin, typ, sub in EXTRA_SECURITIES:
        exch, sym = sid.split(":", 1)
        rows.append((sid, exch, sym, name, isin, None, typ, sub, True, store.company_key(isin, sid), True))
    return store.upsert_many(con, "securities", ("security_id", "exchange", "symbol", "name", "isin", "country",
                                                 "tv_type", "tv_subtype", "is_primary", "company_key", "active"), rows)


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CandidateTests(unittest.TestCase):
    def test_suffix_and_zero_rules(self):
        self.assertEqual(fd.tv_symbol_candidates("0990.HK", "HKG"), ["HKEX:990"])
        self.assertEqual(fd.tv_symbol_candidates("009990.KS", "KSC"), ["KRX:009990"])
        self.assertEqual(fd.tv_symbol_candidates("MCS.NS", "NSE"), ["NSE:MCS"])
        self.assertEqual(fd.tv_symbol_candidates("NRTH.L", "LSE"), ["LSE:NRTH"])
        self.assertEqual(fd.tv_symbol_candidates("SWK.DE", "GER"), ["XETR:SWK"])
        self.assertEqual(fd.tv_symbol_candidates("9901.T", "JPX"), ["TSE:9901"])
        self.assertEqual(fd.tv_symbol_candidates("2991.TW", "TAI"), ["TWSE:2991"])
        self.assertEqual(fd.tv_symbol_candidates("HBDGY", "PNK"), ["OTC:HBDGY"])

    def test_class_variants_and_inference(self):
        self.assertEqual(fd.tv_symbol_candidates("BRK-B", "NYQ")[:2], ["NYSE:BRK-B", "NYSE:BRK.B"])
        self.assertIn("OMXSTO:VOLV_B", fd.tv_symbol_candidates("VOLV-B.ST", "STO")[:2])
        self.assertIn("NSE:BAJAJ_AUTO", fd.tv_symbol_candidates("BAJAJ-AUTO.NS", "NSE"))
        self.assertIn("TSX:RCI.B", fd.tv_symbol_candidates("RCI-B.TO", "TOR"))
        self.assertIn("LSE:AO.", fd.tv_symbol_candidates("AO.L", "LSE"))
        self.assertEqual(fd.tv_symbol_candidates("KRU.JO", None), ["JSE:KRU"])
        self.assertEqual(fd.tv_symbol_candidates("A-R.BK", "SET")[:2], ["SET:A-R", "SET:A"])
        self.assertEqual(fd.tv_symbol_candidates("AAFN0000.CM", "CSE"), ["CSELK:AAF.N0000"])
        self.assertEqual(fd.tv_symbol_candidates("XYZ.Q", "ZZZ"), [])


class NameSimilarityTests(unittest.TestCase):
    def test_legal_suffixes_and_acronyms(self):
        self.assertEqual(fd.name_similarity("Okuzan Motor Corporation", "Okuzan Motor Corp."), 1.0)
        self.assertEqual(fd.name_similarity("Bazaar Group Holding Limited",
                                            "Bazaar Group Holding Limited Sponsored ADR"), 1.0)
        self.assertEqual(fd.name_similarity("Y.A.C. Holdings Co., Ltd.", "Y.A.C.HOLDINGS CO.,LTD."), 1.0)
        self.assertEqual(fd.name_similarity("Jeju Air Co., Ltd.", "JEJUAIR CO., LTD."), 1.0)

    def test_bounds(self):
        self.assertLess(fd.name_similarity("Zeta Systems Holdings Inc.", "Zscaler, Inc."), 0.5)
        self.assertEqual(fd.name_similarity(None, "Apple Inc."), 0.0)
        s = fd.name_similarity("Qingshan Spirits Liquor Co., Ltd.", "Qingshan Spirits Co., Ltd. Class A")
        self.assertTrue(0.5 <= s < 1.0)


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.fd_path = root / "old_fd.duckdb"
        make_fd_duckdb(self.fd_path)
        self.cfg = Config(home=root / "home", fd_duckdb=self.fd_path)
        self.con = store.connect(self.cfg)
        load_fixture_securities(self.con)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def _fd_rows(self):
        rows, _, _ = fd._load_fd_rows(self.fd_path)
        return rows

    def test_filters(self):
        rows, skipped, fetched = fd._load_fd_rows(self.fd_path)
        ids = {r["listing_id"] for r in rows}
        self.assertEqual(len(rows), len(FD_ROWS) - 3)
        self.assertNotIn("FD:XNAS:ORCD", ids)
        self.assertNotIn("FD:XNAS:CHPW", ids)
        self.assertNotIn("FD:XNAS:PDQH", ids)
        self.assertEqual(skipped, {"delisted": 1, "non_equity": 1, "short_summary": 1, "fd_rows_total": 18})
        self.assertEqual(fetched, dt.datetime(2026, 9, 20))

    def test_match_all_securities(self):
        secs = fd._load_securities(self.con, only_universe=False)
        report: dict = {}
        got = {m.security_id: m for m in fd.match(self._fd_rows(), secs, report=report)}
        self.assertEqual(got["HKEX:990"].fd_listing_id, "FD:XHKG:0990.HK")       # zero-stripped ticker beats ISIN
        self.assertEqual(got["HKEX:990"].method, "ticker_exchange")
        self.assertEqual(got["KRX:009990"].fd_listing_id, "FD:XKRX:009990.KS")
        self.assertEqual(got["NSE:MCS"].method, "ticker_exchange")
        self.assertEqual((got["LSE:NRTH"].method, got["LSE:NRTH"].score), ("isin", 1.0))
        self.assertEqual(got["XETR:SWK"].fd_listing_id, "FD:XETR:SWK.DE")
        self.assertEqual(got["OTC:HBDGY"].fd_listing_id, "FD:OTCM:HBDGY")
        self.assertEqual((got["ASX:RDG"].method, got["ASX:RDG"].score), ("isin", 0.9))
        self.assertEqual(got["NYSE:BZAR"].fd_listing_id, "FD:XNYS:BZAR")
        self.assertEqual((got["HKEX:9910"].fd_listing_id, got["HKEX:9910"].score), ("FD:XHKG:9910.HK", 1.0))
        self.assertEqual(got["NYSE:BRK.B"].fd_listing_id, "FD:XNYS:BRK-B")
        self.assertEqual(got["SSE:609999"].method, "ticker_exchange_name_checked")
        self.assertEqual(got["JSE:KRU"].fd_listing_id, "FD:XJSE:KRU.JO")
        for absent in ("NASDAQ:ZNS", "NASDAQ:ORCD", "NASDAQ:CHPW", "NASDAQ:PDQH"):
            self.assertNotIn(absent, got)
        self.assertEqual([r["security_id"] for r in report["name_rejections"]], ["NASDAQ:ZNS"])
        conflicts = {c["fd_listing_id"]: c for c in report["conflicts"]}
        self.assertEqual(set(conflicts), {"FD:OTCM:HBDGY", "FD:XNYS:BZAR"})
        self.assertEqual(conflicts["FD:XNYS:BZAR"]["chosen"], "isin:US0999ZZ1003")
        self.assertEqual(conflicts["FD:XNYS:BZAR"]["rejected"], ["isin:KYG999910004"])
        # Never two company_keys per FD summary.
        per_fd: dict[str, set] = {}
        for m in got.values():
            per_fd.setdefault(m.fd_listing_id, set()).add(m.company_key)
        self.assertTrue(all(len(v) == 1 for v in per_fd.values()))

    def test_import_descriptions(self):
        before = file_sha(self.fd_path)
        out = fd.import_descriptions(self.cfg, self.con)
        self.assertEqual(file_sha(self.fd_path), before)                       # old DuckDB never written
        self.assertEqual(out["fd_rows_considered"], 15)
        self.assertEqual(out["descriptions_written"], 13)
        self.assertEqual(out["matched_by_method"],
                         {"isin": 3, "ticker_exchange": 9, "ticker_exchange_name_checked": 1})
        self.assertEqual(out["conflicts"], 1)                                  # BZAR; HBDGY is not in universe
        self.assertEqual(out["universe_companies"], 19)
        self.assertEqual(out["universe_companies_with_description"], 13)
        self.assertEqual(out["name_rejections"], 1)
        self.assertTrue(out["unmatched_samples"])

        snap = self.con.execute("SELECT source_id, kind, fetched_at, raw_path, raw_sha256, rows FROM snapshots "
                                "WHERE snapshot_id = ?", [out["snapshot_id"]]).fetchone()
        blob = "\n".join(r["summary"] for r in self._fd_rows()).encode()
        self.assertEqual(snap, ("financedatabase_local", "fd_summaries", dt.datetime(2026, 9, 20),
                                str(self.fd_path), hashlib.sha256(blob).hexdigest(), 13))
        self.assertEqual(store.license_tier_for_snapshot(self.con, out["snapshot_id"]), "gray-private")

        row = self.con.execute("SELECT company_key, text, text_sha256, lang, source_url, match_method, match_score "
                               "FROM descriptions WHERE security_id = 'HKEX:990'").fetchone()
        self.assertEqual(row[0], "isin:KYG999901001")
        self.assertTrue(row[1].startswith("Harbour operates"))
        self.assertEqual(row[2], hashlib.sha256(row[1].encode()).hexdigest())
        self.assertEqual(row[3:], ("en", None, "ticker_exchange", 0.95))
        self.assertIsNone(self.con.execute(
            "SELECT 1 FROM descriptions WHERE security_id IN ('OTC:HBDGY', 'NASDAQ:ZNS', 'NASDAQ:ORCD')").fetchone())

    def test_import_is_idempotent(self):
        fd.import_descriptions(self.cfg, self.con)
        fd.import_descriptions(self.cfg, self.con)
        self.assertEqual(self.con.execute("SELECT count(*) FROM descriptions").fetchone()[0], 13)

    def test_reimport_removes_stale_matches(self):
        fd.import_descriptions(self.cfg, self.con)
        fdc = duckdb.connect(str(self.fd_path))
        fdc.execute("UPDATE fd_listing_attributes SET is_delisted = true WHERE listing_id = 'FD:XNSE:MCS.NS'")
        fdc.close()
        out = fd.import_descriptions(self.cfg, self.con)
        self.assertEqual(out["stale_removed"], 1)
        self.assertIsNone(self.con.execute("SELECT 1 FROM descriptions WHERE security_id = 'NSE:MCS'").fetchone())
        self.assertEqual(self.con.execute("SELECT count(*) FROM descriptions").fetchone()[0], 12)


def _fd(lid, ticker, ex, isin, name, summary):
    return {"listing_id": lid, "ticker": ticker, "exchange": ex, "id_isin": isin, "name": name, "summary": summary}


def _sec(sid, name, isin):
    return {"security_id": sid, "name": name, "isin": isin, "company_key": store.company_key(isin, sid)}


class MatchSafetyTests(unittest.TestCase):
    """Regression cases found in an offline run against a full FinanceDatabase table (tiny hand-made rows)."""

    def test_cross_venue_isin_needs_name_floor(self):
        secs = [_sec("HKEX:16", "Sun Hung Kai Properties Limited", "HK0016000132")]
        rows = [_fd("FD:SHG", "SHG", "NYQ", "HK0016000132", "Shinhan Financial Group Co., Ltd.", "Shinhan " + LONG)]
        report: dict = {}
        self.assertEqual(fd.match(rows, secs, report=report), [])
        self.assertEqual(report["name_rejections"][0]["reason"], "isin_name_floor")

    def test_ticker_match_with_contradicting_isin_rejected(self):
        secs = [_sec("BIST:CRFSA", "CarrefourSA Carrefour Sabanci Ticaret Merkezi A.S.", "TRACRFSA92I8")]
        rows = [_fd("FD:CRFSA", "CRFSA.IS", "IST", "US1444302046", "Carrefour SA", "Carrefour " + LONG)]
        report: dict = {}
        self.assertEqual(fd.match(rows, secs, report=report), [])
        self.assertEqual(report["name_rejections"][0]["reason"], "isin_conflict")

    def test_subset_boost_needs_generic_extra_tokens(self):
        self.assertLess(fd.name_similarity("ArcelorMittal", "ArcelorMittal South Africa Ltd"), 0.85)
        self.assertGreaterEqual(fd.name_similarity("Honeywell", "Honeywell International Inc."), 0.85)

    def test_shared_text_goes_to_one_company(self):
        text = "Infosys " + LONG
        secs = [_sec("NSE:INFY", "Infosys Limited", "INE009A01021"),
                _sec("BSE:SOUTHERNIN", "Southern Infoconsultants Ltd", "INE000S01011")]
        rows = [_fd("FD:INFY.NS", "INFY.NS", "NSE", "INE009A01021", "Infosys Limited", text),
                _fd("FD:SOUTHERNIN.BO", "SOUTHERNIN.BO", "BSE", None, "Southern Infoconsultants Limited", text)]
        report: dict = {}
        got = fd.match(rows, secs, report=report)
        self.assertEqual([m.security_id for m in got], ["NSE:INFY"])
        self.assertEqual(report["text_conflicts"][0]["rejected"], ["isin:INE000S01011"])

    def test_shared_text_kept_for_same_named_company(self):
        text = "Bazaar " + LONG
        secs = [_sec("NYSE:BZAR", "Bazaar Group Holding Limited Sponsored ADR", "US0999ZZ1003"),
                _sec("HKEX:9910", "Bazaar Group Holding Ltd.", "KYG999910004")]
        rows = [_fd("FD:BZAR", "BZAR", "NYQ", None, "Bazaar Group Holding Limited", text),
                _fd("FD:9910.HK", "9910.HK", "HKG", "KYG999910004", "Bazaar Group Holding Limited", text)]
        report: dict = {}
        got = fd.match(rows, secs, report=report)
        self.assertEqual([m.security_id for m in got], ["HKEX:9910", "NYSE:BZAR"])
        self.assertEqual(report["text_conflicts"], [])


@unittest.skipUnless(os.environ.get("JEVSCREEN_TEST_REAL_FD") == "1", "set JEVSCREEN_TEST_REAL_FD=1 to run")
class RealFinanceDatabaseTest(unittest.TestCase):
    """Opt-in: match a local FD DuckDB (read-only, JEVSCREEN_FD_DUCKDB) against the store's securities, or the fixtures if empty."""

    def test_real_counts(self):
        real = Config()
        fd_path = real.fd_duckdb
        if not fd_path.exists():
            self.skipTest(f"no FD DuckDB at {fd_path}")
        mtime = fd_path.stat().st_mtime_ns
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(home=Path(tmp), fd_duckdb=fd_path)
            con = store.connect(cfg)
            copied = 0
            if real.db_path.exists():
                try:
                    src = duckdb.connect(str(real.db_path), read_only=True)
                    cur = src.execute("SELECT * FROM securities")
                    cols = [d[0] for d in cur.description]
                    copied = store.upsert_many(con, "securities", cols, cur.fetchall())
                    src.close()
                except duckdb.Error as exc:
                    print(f"could not read {real.db_path}: {exc}")
            if not copied:
                load_fixture_securities(con)
            out = fd.import_descriptions(cfg, con)
            con.close()
        self.assertEqual(fd_path.stat().st_mtime_ns, mtime)
        print("\nreal FD import:", json.dumps({k: v for k, v in out.items() if k != "conflict_samples"},
                                              indent=1, default=str))
        self.assertGreater(out["fd_rows_considered"], 0)


if __name__ == "__main__":
    unittest.main()
