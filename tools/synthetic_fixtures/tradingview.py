"""Synthetic TradingView fixtures: scanner payloads (USD and local mode) and one symbol page.

Every company name, ISIN, figure and sentence here is invented. Tickers copy each exchange's code format; A-share
codes use the unlisted 609xxx block, the others were picked to look unused but are not checked against live
listings, so a code may coincide with a real one: the data attached to it is invented and says nothing about that
issuer (report such a clash and it will be renamed). The payloads only copy the *shape* of the scanner API (column
order in tests/fixtures/tv_columns.json, the usd/local pairing, null patterns, history lengths), which is what the
tests exercise. Run: python3 tools/synthetic_fixtures/tradingview.py
"""
from __future__ import annotations

import json
import random
from pathlib import Path

FIX = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
COLUMNS = json.loads((FIX / "tv_columns.json").read_text())

MAR31_2026, DEC31_2025, SEP30_2025, JUN30_2026, JAN31_2026 = 1774915200, 1767139200, 1759190400, 1782777600, 1785456000

# (symbol, name, type, subtype, is_primary, country, isin, price ccy, fundamental ccy, fx to USD,
#  fiscal period end, history length, first history year, sector, industry)
ROWS = [
    ("TSE:9901", "Okuzan Motor Corp.", "stock", "common", True, "Japan", "JP3999010001", "JPY", "JPY", 0.0063,
     MAR31_2026, 20, 2025, "Consumer Durables", "Motor Vehicles"),
    ("TWSE:2991", "Formosa Wafer Manufacturing Co., Ltd.", "stock", "common", True, "Taiwan", "TW0002991009", "TWD",
     "TWD", 0.0314, DEC31_2025, 20, 2025, "Electronic Technology", "Semiconductors"),
    ("XETR:SWK", "Softwerk SE", "stock", "common", True, "Germany", "DE000SWK0019", "EUR", "EUR", 1.1377,
     DEC31_2025, 20, 2025, "Technology Services", "Packaged Software"),
    ("NSE:MCS", "Meru Consultancy Services Limited", "stock", "common", True, "India", "INE999M01011", "INR", "INR",
     0.0104, MAR31_2026, 20, 2025, "Technology Services", "Information Technology Services"),
    ("HKEX:990", "Harbour Digital Holdings Ltd", "stock", "common", True, "China", "KYG999901001", "HKD", "HKD",
     0.1275, DEC31_2025, 20, 2025, "Technology Services", "Packaged Software"),
    ("NYSE:BZAR", "Bazaar Group Holding Limited Sponsored ADR", "dr", "", True, "China", "US0999ZZ1003", "USD", "USD",
     1.0, MAR31_2026, 15, 2025, "Retail Trade", "Internet Retail"),
    ("KRX:009990", "Hanbit Electronics Co., Ltd.", "stock", "common", True, "South Korea", "KR7009990000", "KRW",
     "KRW", 0.00072, DEC31_2025, 20, 2025, "Electronic Technology", "Semiconductors"),
    ("NASDAQ:ORCD", "Orchard Devices Inc.", "stock", "common", True, "United States", "US68599X1000", "USD", "USD",
     1.0, SEP30_2025, 20, 2025, "Electronic Technology", "Telecommunications Equipment"),
    ("LSE:NRTH", "Northsea Energy Plc", "stock", "common", True, "United Kingdom", "GB00BZ9NRT01", "GBX", "GBP",
     1.321, DEC31_2025, 20, 2025, "Energy Minerals", "Integrated Oil"),
    ("TASE:GLPH", "Galil Pharmaceutical Industries Limited", "stock", "common", True, "Israel", "IL0009990011", "ILA",
     "ILS", 0.2981, DEC31_2025, 20, 2025, "Health Technology", "Pharmaceuticals: Generic"),
    ("JSE:KRU", "Karoo Media Limited Class N", "stock", "common", True, "South Africa", "ZAE000999011", "ZAC", "ZAR",
     0.0561, MAR31_2026, 20, 2025, "Technology Services", "Internet Software/Services"),
    ("BMFBOVESPA:MARE4", "Mare Petroleo SA Pfd", "stock", "preferred", False, "Brazil", "BRMAREACNPR3", "BRL", "BRL",
     0.1822, DEC31_2025, 20, 2025, "Energy Minerals", "Integrated Oil"),
    ("TSX:TILL", "Till Commerce, Inc. Class A", "stock", "common", False, "Canada", "CA88599T1003", "CAD", "CAD",
     0.7131, DEC31_2025, 14, 2025, "Commercial Services", "Miscellaneous Commercial Services"),
    ("ASX:RDG", "Red Gum Resources Ltd", "stock", "common", True, "Australia", "AU000000RDG5", "AUD", "AUD", 0.6573,
     JUN30_2026, 20, 2026, "Non-Energy Minerals", "Steel"),
    ("SSE:609999", "Qingshan Spirits Co., Ltd. Class A", "stock", "common", True, "China", "CNE000009991", "CNY", "CNY",
     0.1405, DEC31_2025, 20, 2025, "Consumer Non-Durables", "Beverages: Alcoholic"),
    ("NASDAQ:CHPW", "Chipwright Holdings PLC Sponsored ADR", "dr", "", True, "United Kingdom", "US17099W1009", "USD",
     "USD", 1.0, MAR31_2026, 6, 2025, "Electronic Technology", "Semiconductors"),
    ("NASDAQ:PDQH", "PDQ Holdings Inc. Sponsored ADR Class A", "dr", "", True, "Ireland", "US69399Q1004", "USD", "USD",
     1.0, DEC31_2025, 9, 2025, "Retail Trade", "Department Stores"),
    ("EURONEXT:VLB", "Vlaander Bank N.V. Depositary receipts", "dr", "", True, "Netherlands", "NL0099990013", "EUR",
     "EUR", 1.1377, DEC31_2025, 16, 2025, "Finance", "Major Banks"),
    ("OTC:HBDGY", "Harbour Digital Holdings Ltd Unsponsored ADR", "dr", "", False, "China", "US41199H1005", "USD",
     "USD", 1.0, DEC31_2025, 20, 2025, "Technology Services", "Packaged Software"),
    ("NASDAQ:ZNS", "Zenscale, Inc.", "stock", "common", True, "United States", "US98999Z1008", "USD", "USD", 1.0,
     JAN31_2026, 12, 2026, "Technology Services", "Packaged Software"),
]

# Null patterns the tests rely on (missing values must stay NULL, never 0).
NULL_CURRENT = {
    "HKEX:990": {"free_cash_flow_ttm"}, "NASDAQ:PDQH": {"free_cash_flow_ttm"}, "OTC:HBDGY": {"free_cash_flow_ttm"},
    "EURONEXT:VLB": {"float_shares_outstanding", "gross_profit_ttm", "free_cash_flow_ttm"},
    "TWSE:2991": {"number_of_employees"},
}
NULL_HISTORY = {"EURONEXT:VLB": {"gross_profit_fy_h", "ebitda_fy_h"}}           # whole array missing
NULL_HISTORY_CELL = {"BMFBOVESPA:MARE4": {"earnings_per_share_diluted_fy_h": -1},  # one null value inside an array
                     "TSX:TILL": {"total_assets_fy_h": -1, "total_debt_fy_h": -1},
                     "NASDAQ:CHPW": {"total_assets_fy_h": -1, "total_debt_fy_h": -1},
                     "NASDAQ:ZNS": {"total_assets_fy_h": -1, "total_debt_fy_h": -1}}
LOSS_MAKERS = {"NASDAQ:ZNS"}                  # negative net income
NEGATIVE_FCF = {"TSE:9901", "NYSE:BZAR"}      # negative free cash flow
ADR_OF = {"OTC:HBDGY": "HKEX:990"}           # same company: shares and USD market cap copied from the ordinary line


def _round(x: float) -> float:
    return float(round(x))


def build() -> tuple[dict, dict]:
    rng = random.Random(20260926)
    usd_rows, loc_rows, by_sid = [], [], {}
    for (sid, name, typ, sub, prim, country, isin, pccy, fccy, fx, fy_end, hlen, y0, sector, industry) in ROWS:
        exch, sym = sid.split(":", 1)
        rev_usd = rng.choice([2, 5, 9, 14, 30, 60, 110, 250]) * 1e9 * (1 + rng.random())
        margin = -0.02 if sid in LOSS_MAKERS else 0.05 + 0.25 * rng.random()
        cap_usd = rev_usd * (1.5 + 6 * rng.random())
        shares = float(rng.randrange(150, 12000)) * 1e6
        cur_usd = {
            "market_cap_basic": _round(cap_usd), "total_revenue_ttm": _round(rev_usd),
            "net_income_ttm": _round(rev_usd * margin), "gross_profit_ttm": _round(rev_usd * (0.2 + 0.5 * rng.random())),
            "free_cash_flow_ttm": _round(rev_usd * (-0.03 if sid in NEGATIVE_FCF else 0.1 * rng.random() + 0.02)),
            "total_revenue_fy": _round(rev_usd * 0.94), "net_income_fy": _round(rev_usd * margin * 0.9),
        }
        if sid in ADR_OF:
            base = by_sid[ADR_OF[sid]]
            cur_usd["market_cap_basic"], shares = base["usd"]["market_cap_basic"], base["usd"]["total_shares_outstanding"]
        common = {
            "name": sym, "description": name, "type": typ, "subtype": sub, "is_primary": prim, "exchange": exch,
            "country": country, "isin": isin, "currency": pccy, "close": round(cap_usd / shares / fx, 2),
            "volume": rng.randrange(1_000_000, 30_000_000), "average_volume_10d_calc": rng.randrange(1_000_000, 30_000_000),
            "total_shares_outstanding": shares, "float_shares_outstanding": round(shares * (0.3 + 0.7 * rng.random())),
            "sector": sector, "industry": industry, "number_of_employees": rng.randrange(2_000, 400_000),
            "fiscal_period_end_fy": fy_end,
        }
        periods = list(range(y0, y0 - hlen, -1))
        hist = {"fiscal_period_fy_h": periods}
        rev_loc = rev_usd / fx
        for m in ("total_revenue", "net_income", "gross_profit", "ebitda", "free_cash_flow", "total_assets",
                  "total_debt", "earnings_per_share_diluted"):
            scale = {"total_revenue": 1, "net_income": margin, "gross_profit": 0.4, "ebitda": 0.25,
                     "free_cash_flow": 0.08, "total_assets": 2.2, "total_debt": 0.5,
                     "earnings_per_share_diluted": margin / shares}[m]
            vals = []
            for i, _ in enumerate(periods):
                v = rev_loc * scale * (0.93 ** i) * (0.9 + 0.2 * rng.random())
                vals.append(round(v, 2) if m == "earnings_per_share_diluted" else _round(v))
            hist[f"{m}_fy_h"] = vals
        for col in NULL_HISTORY.get(sid, ()):
            hist[col] = None
        for col, idx in NULL_HISTORY_CELL.get(sid, {}).items():
            hist[col][idx] = None
        usd = {**common, "fundamental_currency_code": "USD", **cur_usd, **hist}
        loc = {**common, "fundamental_currency_code": fccy, **hist,
               **{k: (v if fccy == "USD" else _round(v / fx)) for k, v in cur_usd.items()}}
        for col in NULL_CURRENT.get(sid, ()):
            usd[col] = loc[col] = None
        by_sid[sid] = {"usd": usd}
        usd_rows.append({"s": sid, "d": [usd[c] for c in COLUMNS]})
        loc_rows.append({"s": sid, "d": [loc[c] for c in COLUMNS]})
    return {"totalCount": len(usd_rows), "data": usd_rows}, {"totalCount": len(loc_rows), "data": loc_rows}


def _faq(q: str, a: str) -> dict:
    return {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}}


def profile_page() -> str:
    name, sym, url = "Okuzan Motor Corp.", "9901", "https://www.tradingview.com/symbols/TSE-9901/"
    desc = ("Okuzan Motor Corporation (Okuzan) designs, assembles and sells passenger cars, light trucks and parts "
            "under its own brands. The Automotive segment builds vehicles in four countries and sells them through "
            "dealer networks. The Mobility Finance segment offers retail loans and leases to buyers of its vehicles. "
            "The company was founded in 1952 and is headquartered in Kofu, Japan. (Synthetic test text.)")
    product = {"@context": "https://schema.org", "@type": "FinancialProduct", "name": name, "description": desc,
               "category": "Stock", "tickerSymbol": sym,
               "identifier": [{"@type": "PropertyValue", "propertyID": "tickerSymbol", "value": sym},
                              {"@type": "PropertyValue", "propertyID": "ISIN", "value": "JP3999010001"}],
               "provider": {"@type": "Organization", "name": "TradingView", "url": "https://www.tradingview.com"},
               "offers": {"@type": "Offer", "price": "1234.5", "priceCurrency": "JPY",
                          "priceValidUntil": "2026-09-25T06:30:01Z"}}
    org = {"@context": "https://schema.org", "@type": "Organization", "name": "TradingView",
           "url": "https://www.tradingview.com"}
    dataset = {"@context": "https://schema.org", "@type": "Dataset", "name": f"{sym} price data",
               "description": f"Historical price data for {name} (synthetic)."}
    crumbs = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": i + 1, "item": {"@id": u, "name": n}} for i, (u, n) in enumerate([
            ("https://www.tradingview.com/markets/", "Markets"),
            ("https://www.tradingview.com/markets/japan/", "Japan"),
            (url, sym)])]}
    faq = {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
        _faq(f"What is {name} stock ticker?",
             f"The ticker is {sym}. See the <a href=\"/chart/?symbol=TSE:{sym}\">{sym} chart</a>."),
        _faq(f"Does {name} release reports?",
             f"Yes, see <a href=\"/symbols/TSE-{sym}/financials-income-statement/\">{name} financials</a>."),
        _faq(f"How many employees does {name} have?", "About 12,000 people (synthetic)."),
    ]}
    blocks = "\n".join(f'<script type="application/ld+json">{json.dumps(b, indent=2, ensure_ascii=False)}</script>'
                       for b in (product, org, dataset, crumbs, faq))
    return (f"<!doctype html><html><head><title>synthetic symbol page</title>\n{blocks}\n</head>"
            f"<body>synthetic fixture shaped like {url}</body></html>\n")


def main() -> None:
    usd, loc = build()
    (FIX / "tv_scan_usd.json").write_text(json.dumps(usd, separators=(",", ":")) + "\n")
    (FIX / "tv_scan_local.json").write_text(json.dumps(loc, separators=(",", ":")) + "\n")
    (FIX / "tv_profile_TSE-9901.html").write_text(profile_page(), encoding="utf-8")


if __name__ == "__main__":
    main()
