"""Synthetic store additions for the shells / ledger / why / sieve-author tests (not a test module itself).

Every issuer here is invented. seed_why() runs on top of test_screen.seed() and adds:
- two blank-check shells (a name hit and a profile-text hit), a 'formerly a blank check' operating company, and an
  operating company that kept 'Acquisition Corp' in its name (industry guard -> spac_like flag only);
- two A shares with a CNINFO stock list (ST and *ST short names) joined through identifiers.cninfo_orgid;
- the alias lists `jevscreen why` reads: an EDINET code list, a DART corp code list and SEC tickers.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import zipfile
from pathlib import Path

from jevscreen import store

D = dt.date(2026, 9, 26)
SEC_COLS = ("security_id", "exchange", "symbol", "name", "isin", "country", "tv_type", "tv_subtype", "is_primary",
            "company_key", "last_seen_snapshot", "active", "industry")

LINES = [
    # security_id, name, isin, country, industry, mcap, revenue ttm, description
    ("NASDAQ:FXAC", "Fernhollow Acquisition Corp.", "US0000000101", "United States", "Financial Conglomerates", 3e8,
     0.0, "Fernhollow Acquisition Corp. intends to effect a merger with one or more businesses."),
    ("NYSE:QMRG", "Quillmere Holdings Inc.", "US0000000102", "United States", None, 4e8, None,
     "Quillmere Holdings Inc. is a blank check company formed to effect a business combination."),
    ("NASDAQ:BVRB", "Brightvane Robotics Inc.", "US0000000103", "United States", "Industrial Machinery", 6e8, 9e7,
     "Brightvane Robotics Inc., formerly a blank check company, makes robot grippers for warehouses."),
    ("NYSE:KPAC", "Kestrelpoint Acquisition Corp", "US0000000104", "United States", "Industrial Machinery", 7e8,
     5e8, "Kestrelpoint Acquisition Corp is a blank check company that acquired a robot welding cell maker."),
    ("SZSE:309901", "Shixin Humanoid Tech Co., Ltd. Class A", "CNE000009901", "China", "Industrial Machinery", 9e8,
     None, "Shixin Humanoid Tech makes humanoid robots for factory assembly."),
    ("SSE:609902", "Jiaxin Textile Co., Ltd. Class A", "CNE000009902", "China", "Textiles", 5e8, None,
     "Jiaxin Textile makes polyester yarn."),
]
STOCK_LIST = {"stockList": [
    {"code": "309901", "pinyin": "stsx", "category": "A股", "orgId": "9900099011", "zwjc": "ST示信"},
    {"code": "609902", "pinyin": "stjx", "category": "A股", "orgId": "9900099022", "zwjc": "*ST甲信"},
    {"code": "309903", "pinyin": "yxkj", "category": "A股", "orgId": "9900099033", "zwjc": "乙信科技"},
    # the same issuer's B share: one org_id, two codes (the A line must keep its own name)
    {"code": "209901", "pinyin": "sxb", "category": "B股", "orgId": "9900099011", "zwjc": "示信B"},
]}


def _zip(name: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, data)
    return buf.getvalue()


def seed_why(cfg, home: Path, *, st_fetched: dt.datetime | None = None, stock_list: bool = True,
             alias_lists: bool = True) -> None:
    raw = home / "rawfix"
    raw.mkdir(parents=True, exist_ok=True)
    with store.session(cfg) as con:
        scan = con.execute("SELECT snapshot_id FROM snapshots WHERE source_id = 'tradingview_scanner' LIMIT 1"
                           ).fetchone()[0]
        prof = con.execute("SELECT snapshot_id FROM snapshots WHERE source_id = 'tradingview_profile' LIMIT 1"
                           ).fetchone()[0]
        rows, mkt, fund, desc = [], [], [], []
        for sid, name, isin, country, industry, mcap, rev, text in LINES:
            ex, sym = sid.split(":")
            rows.append((sid, ex, sym, name, isin, country, "stock", "common", True, store.company_key(isin, sid),
                         scan, True, industry))
            mkt.append((sid, D, mcap, 1e6, scan))
            if rev is not None:
                fund.append((sid, D, "total_revenue", "ttm", rev, rev, "USD", scan))
            desc.append((sid, "tradingview_profile", store.company_key(isin, sid), text, prof))
        store.upsert_many(con, "securities", SEC_COLS, rows)
        store.upsert_many(con, "market_daily", ("security_id", "as_of", "market_cap_usd", "avg_volume_10d",
                                                "snapshot_id"), mkt)
        store.upsert_many(con, "fundamentals_current", ("security_id", "as_of", "metric", "period", "value_usd",
                                                        "value_local", "currency_local", "snapshot_id"), fund)
        store.upsert_many(con, "descriptions", ("security_id", "source_id", "company_key", "text", "snapshot_id"),
                          desc)
        ids = []
        if stock_list:
            p = raw / "stock_list.json"
            p.write_text(json.dumps(STOCK_LIST, ensure_ascii=False), encoding="utf-8")
            snap = store.record_snapshot(con, source_id="cninfo_annual_report", kind="stock_list", request=None,
                                         raw_path=str(p), raw_sha256=None, raw_bytes=None, rows=3, duration_s=None,
                                         fetched_at=st_fetched or dt.datetime(2026, 9, 26, 12, 0))
            ids += [("SZSE:309901", "cninfo_orgid", "9900099011", "test", snap),
                    ("SSE:609902", "cninfo_orgid", "9900099022", "test", snap)]
        if alias_lists:
            # EDINET code list (CSV in a zip, header on the second row as in the real download)
            csv_text = ("ダウンロード実行日,2026年09月26日現在,件数,1件\n"
                        "ＥＤＩＮＥＴコード,提出者種別,上場区分,連結の有無,資本金,決算日,提出者名,提出者名（英字）,"
                        "提出者名（ヨミ）,所在地,提出者業種,証券コード,提出者法人番号\n"
                        "E99001,内国法人・組合,上場,有,100,3月31日,テスト工業株式会社,Test Kogyo Co. Ltd.,"
                        "テストコウギョウ,東京都,機械,60000,1234567890123\n")
            p = raw / "edinet_codelist.zip"
            p.write_bytes(_zip("EdinetcodeDlInfo.csv", csv_text.encode("utf-8")))
            snap = store.record_snapshot(con, source_id="edinet_yuho", kind="edinet_codelist", request=None,
                                         raw_path=str(p), raw_sha256=None, raw_bytes=None, rows=1, duration_s=None)
            ids.append(("TSE:6000", "edinet_code", "E99001", "test", snap))
            xml = ("<result><list><corp_code>00999001</corp_code><corp_name>테스트로봇</corp_name>"
                   "<corp_eng_name>Test Robot Co</corp_eng_name><stock_code>999001</stock_code>"
                   "<modify_date>20260101</modify_date></list></result>")
            p = raw / "dart_corpcode.zip"
            p.write_bytes(_zip("CORPCODE.xml", xml.encode("utf-8")))
            snap = store.record_snapshot(con, source_id="dart_business_report", kind="dart_corpcode", request=None,
                                         raw_path=str(p), raw_sha256=None, raw_bytes=None, rows=1, duration_s=None)
            ids.append(("LSE:GEAR", "dart_corp_code", "00999001", "test", snap))
            p = raw / "company_tickers_exchange.json"
            p.write_text(json.dumps({"fields": ["cik", "name", "ticker", "exchange"],
                                     "data": [[777, "Robo Two Holdings", "ROB2", "Nasdaq"]]}), encoding="utf-8")
            store.record_snapshot(con, source_id="sec_tickers", kind="company_tickers_exchange", request=None,
                                  raw_path=str(p), raw_sha256=None, raw_bytes=None, rows=1, duration_s=None)
        if ids:
            store.upsert_many(con, "identifiers", ("security_id", "id_type", "id_value", "method", "snapshot_id"),
                              ids)
