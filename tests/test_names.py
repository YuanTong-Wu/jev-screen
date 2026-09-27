"""Tests for jevscreen.names (alias index and resolver) on a synthetic store with invented issuers."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import names, store  # noqa: E402
from test_screen import StoreCase  # noqa: E402
from why_seed import seed_why  # noqa: E402


class TestNormalize(unittest.TestCase):
    def test_suffixes_and_scripts(self):
        self.assertEqual(names.normalize("RoboCorp, Inc."), "robocorp")
        self.assertEqual(names.normalize("Shixin Humanoid Tech Co., Ltd. Class A"), "shixinhumanoidtech")
        self.assertEqual(names.normalize("テスト工業株式会社"), "テスト工業")
        self.assertEqual(names.normalize("(주)테스트로봇"), "테스트로봇")
        self.assertEqual(names.normalize("示信科技股份有限公司"), "示信科技")
        self.assertEqual(names.normalize("ＲＯＢＯ　Ｃｏｒｐ"), "robo")


class IndexCase(StoreCase):
    def setUp(self):
        super().setUp()
        seed_why(self.cfg, self.home)
        with store.session(self.cfg) as con:
            # a second, non-primary line of RoboCorp (same company_key): resolves to the universe line
            con.execute("INSERT INTO securities (security_id, exchange, symbol, name, isin, country, tv_type, "
                        "tv_subtype, is_primary, company_key, active) VALUES ('NYSE:ROBO.B', 'NYSE', 'ROBO.B', "
                        "'RoboCorp Class B', NULL, 'United States', 'stock', 'common', false, 'isin:US0000000001', "
                        "true)")
        self.index = self.build()

    def build(self):
        with store.session(self.cfg, read_only=True) as con:
            return names.alias_index(con)

    def r(self, text, **kw):
        return names.resolve(self.index, text, **kw)


class TestResolve(IndexCase):
    def test_ids_symbols_codes(self):
        m = self.r("NYSE:ROBO")
        self.assertEqual((m.status, m.security_id, m.method, m.score), ("exact", "NYSE:ROBO", "id", 1.0))
        self.assertEqual(self.r("robo").method, "symbol")
        self.assertEqual(self.r("309901").security_id, "SZSE:309901")
        m = self.r("isin:US0000000007")
        self.assertEqual((m.security_id, m.method), ("NASDAQ:ROB2", "company_key"))
        self.assertTrue(m.in_universe)

    def test_non_universe_line(self):
        m = self.r("ROBO.B")
        self.assertEqual((m.status, m.security_id, m.matched_line), ("exact", "NYSE:ROBO", "NYSE:ROBO.B"))

    def test_zh_st_alias(self):
        for q in ("ST示信", "示信", "ＳＴ示信"):
            m = self.r(q)
            self.assertEqual((m.status, m.security_id), ("exact", "SZSE:309901"), q)
            self.assertEqual(m.method, "alias:cninfo")
        self.assertEqual(self.r("*ST甲信").security_id, "SSE:609902")
        m = self.r("北示信")                       # containment 2/3 < 0.8: not unique, but listed
        self.assertEqual(m.status, "not_found")
        self.assertEqual(m.candidates[0]["security_id"], "SZSE:309901")
        self.assertEqual(m.candidates[0]["method"], "contains:cninfo")
        self.assertAlmostEqual(m.candidates[0]["score"], 0.6667, places=3)

    def test_ja_ko_sec_aliases(self):
        m = self.r("テスト工業")
        self.assertEqual((m.status, m.security_id, m.method), ("exact", "TSE:6000", "alias:edinet"))
        self.assertEqual(self.r("Test Kogyo").security_id, "TSE:6000")
        m = self.r("테스트로봇")
        self.assertEqual((m.security_id, m.method), ("LSE:GEAR", "alias:dart"))
        m = self.r("Robo Two Holdings")
        self.assertEqual(m.security_id, "NASDAQ:ROB2")
        self.assertEqual(sorted(self.index.sources_used), ["cninfo", "dart", "edinet", "sec", "securities"])

    def test_fuzzy_and_not_found(self):
        m = self.r("RoboCorps")
        self.assertEqual((m.status, m.security_id), ("fuzzy", "NYSE:ROBO"))
        self.assertLess(m.score, 1.0)
        m = self.r("Nonexistent Widgets")
        self.assertEqual((m.status, m.candidates), ("not_found", []))
        self.assertEqual(self.r("").status, "not_found")

    def test_ambiguous_and_prefer(self):
        idx = names.Index(
            lines={"TSE:9991": {"company_key": "a", "name": "テスト工業", "symbol": "9991", "exchange": "TSE",
                                "active": True},
                   "TSE:9992": {"company_key": "b", "name": "テスト紡織", "symbol": "9992", "exchange": "TSE",
                                "active": True}},
            universe={"a": "TSE:9991", "b": "TSE:9992"}, by_id={"tse:9991": "TSE:9991", "tse:9992": "TSE:9992"},
            by_symbol={"9991": ["TSE:9991"], "9992": ["TSE:9992"]},
            aliases=[names.Alias("テスト工業", "テスト工業", "TSE:9991", "a", "edinet"),
                     names.Alias("テスト紡織", "テスト紡織", "TSE:9992", "b", "edinet")], by_norm={},
            sources_used=["edinet"])
        m = names.resolve(idx, "テスト")
        self.assertEqual(m.status, "ambiguous")
        self.assertEqual([c["security_id"] for c in m.candidates], ["TSE:9991", "TSE:9992"])
        self.assertEqual(names.resolve(idx, "テスト", prefer={"b"}).status, "ambiguous")   # 0.6 < 0.8 anyway
        idx.by_symbol["7777"] = ["TSE:9991", "TSE:9992"]        # one code on two exchanges: the run's line wins
        self.assertEqual(names.resolve(idx, "7777").status, "ambiguous")
        m = names.resolve(idx, "7777", prefer={"b"})
        self.assertEqual((m.status, m.security_id), ("exact", "TSE:9992"))

    def test_missing_lists_degrade(self):
        with store.session(self.cfg) as con:
            con.execute("DELETE FROM snapshots WHERE kind IN ('stock_list', 'edinet_codelist', 'dart_corpcode', "
                        "'company_tickers_exchange')")
        idx = self.build()
        self.assertEqual(idx.sources_used, ["securities"])
        self.assertEqual(names.resolve(idx, "示信").status, "not_found")
        self.assertEqual(names.resolve(idx, "NYSE:ROBO").status, "exact")


if __name__ == "__main__":
    unittest.main()
