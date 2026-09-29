"""Coverage report for the jev-screen data layer.

Rules (see docs/DATA_RULES.md):
- Everything is counted from the `universe` view, i.e. one row per company (company_key), never per listing line.
- A value counts as present only when it is non-NULL; missing stays missing, it is never treated as 0.
- Descriptions are company-level: a description on any line of the same company_key counts for that company.
- Licence tiers are derived row -> snapshot -> source -> tier, never hardcoded; a row whose snapshot is NULL or
  unresolvable is reported under 'unknown' (a provenance gap), never hidden.
- Region: the country (TradingView = country of incorporation) unless it is an offshore jurisdiction or NULL, in
  which case the listing exchange decides (region_for).
- SEC documents (documents table) are company-level too and count only when their text was extracted.
- Official documents from any source (SEC, CNINFO, EDINET, DART, MOPS, BSE; OFFICIAL_DOC_SOURCES) are counted the same way:
  'official_document' per region, plus companies with official text by source and by region x source. A document
  reaches a company by security_id, company_key, or the line's native identifier (sec_cik / cninfo_orgid /
  edinet_code / dart_corp_code / mops_co_id / bse_scrip_code; _NATIVE_IDS) -> documents.cik of that source.
- report() only reads the store with SQL, so it runs on a read-only connection (cli coverage uses one).
"""
from __future__ import annotations

from typing import Any

REGIONS: tuple[str, ...] = (
    "US", "Canada", "China", "Hong Kong", "Japan", "South Korea", "Taiwan", "India", "South Asia (other)",
    "Europe & UK", "Australia & NZ", "Southeast Asia", "Other Asia", "Latin America", "Middle East & Africa",
    "Offshore / Caribbean", "Other",
)

OFFSHORE = "Offshore / Caribbean"

# Country names as TradingView spells them (plus common variants). Checked against the live universe on 2026-09-26.
_GROUPS: dict[str, tuple[str, ...]] = {
    "US": ("United States", "USA"),
    "Canada": ("Canada",),
    "China": ("China", "Mainland China"),
    "Hong Kong": ("Hong Kong", "Macau", "Macao"),
    "Japan": ("Japan",),
    "South Korea": ("South Korea", "Korea", "Korea, Republic of"),
    "Taiwan": ("Taiwan",),
    "India": ("India",),
    "South Asia (other)": ("Pakistan", "Bangladesh", "Sri Lanka", "Nepal"),
    "Europe & UK": (
        "United Kingdom", "Ireland", "Germany", "France", "Netherlands", "Belgium", "Luxembourg", "Switzerland",
        "Austria", "Italy", "Spain", "Portugal", "Greece", "Cyprus", "Malta", "Denmark", "Sweden", "Norway",
        "Finland", "Iceland", "Poland", "Czech Republic", "Czechia", "Slovakia", "Hungary", "Romania", "Bulgaria",
        "Croatia", "Slovenia", "Serbia", "Estonia", "Latvia", "Lithuania", "Ukraine", "Russia", "Russian Federation",
        "Monaco", "Liechtenstein", "Jersey", "Guernsey", "Isle of Man", "Gibraltar", "Faroe Islands", "Aland Islands",
        "Greenland", "La Reunion", "Bosnia and Herzegovina", "North Macedonia", "Macedonia", "Montenegro", "Moldova",
        "Belarus", "Georgia", "Armenia"),
    "Australia & NZ": ("Australia", "New Zealand", "Papua New Guinea", "Fiji"),
    "Southeast Asia": (
        "Singapore", "Malaysia", "Indonesia", "Thailand", "Philippines", "Vietnam", "Viet Nam", "Cambodia",
        "Laos", "Myanmar", "Brunei"),
    "Other Asia": ("Kazakhstan", "Mongolia", "Azerbaijan", "Uzbekistan", "Kyrgyzstan"),
    "Latin America": (
        "Mexico", "Brazil", "Argentina", "Chile", "Colombia", "Peru", "Uruguay", "Paraguay", "Venezuela", "Ecuador",
        "Bolivia", "Costa Rica", "Guatemala", "Honduras", "El Salvador", "Trinidad and Tobago",
        "Dominican Republic"),
    "Middle East & Africa": (
        "Israel", "Saudi Arabia", "United Arab Emirates", "Qatar", "Kuwait", "Bahrain", "Oman", "Jordan", "Lebanon",
        "Turkey", "Türkiye", "Iran", "Iraq", "Egypt", "Morocco", "Tunisia", "Algeria", "Nigeria", "Kenya", "Ghana",
        "South Africa", "Namibia", "Botswana", "Zambia", "Zimbabwe", "Mauritius", "Tanzania", "Uganda", "Rwanda",
        "Ethiopia", "Ivory Coast", "Côte d'Ivoire", "Senegal", "Malawi", "Togo", "Gabon", "Mozambique", "Liberia",
        "Sudan"),
    # Incorporation havens. TradingView's `country` is the country of incorporation, so a Cayman-incorporated
    # Chinese company or a Bermuda-incorporated insurer lands here. The listing venue is a better region signal:
    # region_for() falls back to REGION_OF_EXCHANGE for these (and for NULL countries) and only uses this bucket
    # when the exchange is unknown too.
    OFFSHORE: (
        "Bermuda", "Cayman Islands", "British Virgin Islands", "Bahamas", "Panama", "Barbados", "Jamaica",
        "Puerto Rico", "Guam", "Curacao", "Turks and Caicos Islands", "Marshall Islands"),
}

#: TradingView `country` value -> region. Unknown or NULL countries map to "Other" via region_of().
REGION_OF_COUNTRY: dict[str, str] = {c: region for region, names in _GROUPS.items() for c in names}
OFFSHORE_COUNTRIES: frozenset[str] = frozenset(_GROUPS[OFFSHORE])

_EXCHANGES: dict[str, tuple[str, ...]] = {
    "US": ("NASDAQ", "NYSE", "AMEX", "OTC", "CBOE"),
    "Canada": ("TSX", "TSXV", "CSE", "NEO"),
    "China": ("SSE", "SZSE", "BJSE"),
    "Hong Kong": ("HKEX",),
    "Japan": ("TSE", "NAG", "FSE", "SAPSE"),          # FSE = Fukuoka on TradingView (Frankfurt is FWB)
    "South Korea": ("KRX",),
    "Taiwan": ("TWSE", "TPEX"),
    "India": ("NSE", "BSE"),                          # BSE = Bombay
    "South Asia (other)": ("PSX", "DSEBD", "CSELK"),
    "Europe & UK": (
        "LSE", "AQUIS", "EURONEXT", "XETR", "FWB", "DUS", "HAM", "MUN", "BER", "SWB", "LS", "GETTEX", "MIL", "BME",
        "SIX", "BX", "VIE", "OMXSTO", "NGM", "OMXCOP", "OMXHEX", "OMXICE", "OMXTSE", "OMXRSE", "OMXVSE", "OSL", "GPW",
        "NEWCONNECT", "BVB", "BSESOF", "ZSE", "BET", "ATHEX", "PSECZ", "BSSE", "LJSE", "BELEX", "LUXSE", "CSECY",
        "RUS", "MOEX"),
    "Australia & NZ": ("ASX", "NZX"),
    "Southeast Asia": ("SGX", "MYX", "SET", "IDX", "PSE", "HOSE", "HNX", "UPCOM"),
    "Latin America": ("BMFBOVESPA", "BMV", "BIVA", "BCBA", "BCS", "BVL", "BVC", "BVCV"),
    "Middle East & Africa": (
        "TASE", "BIST", "TADAWUL", "ADX", "DFM", "NASDAQDUBAI", "QSE", "KSE", "BAHRAIN", "EGX", "JSE", "NSENG",
        "NSEKE", "CSEMA", "BVMT"),
}

#: TradingView exchange code -> region of the listing venue (used for offshore/NULL countries). Codes seen live
#: on 2026-09-26 plus a few German regional venues; an unknown code simply yields None.
REGION_OF_EXCHANGE: dict[str, str] = {e: region for region, codes in _EXCHANGES.items() for e in codes}

METRICS: tuple[str, ...] = ("companies", "market_cap", "ttm_revenue", "revenue_fy3", "revenue_fy10", "description",
                            "sec_document", "official_document")
#: Official annual-report sources (documents.source_id) counted as 'official_document', always listed (0 if absent).
OFFICIAL_DOC_SOURCES: tuple[str, ...] = ("sec_filing_text", "cninfo_annual_report", "edinet_yuho",
                                         "dart_business_report", "mops_annual_report", "bse_annual_report")
_NATIVE_IDS: dict[str, str] = {"sec_cik": "sec_filing_text", "cninfo_orgid": "cninfo_annual_report",
                               "edinet_code": "edinet_yuho", "dart_corp_code": "dart_business_report",
                               "mops_co_id": "mops_annual_report", "bse_scrip_code": "bse_annual_report"}
#: Description sources always listed in description_by_source (0 when absent), others appear when present.
DESCRIPTION_SOURCES: tuple[str, ...] = ("financedatabase_local", "sec_filing_text", "tradingview_profile")
GAP_LIMIT = 20


def region_of(country: str | None) -> str:
    """Map a TradingView country name to a region; unknown or None -> 'Other'."""
    if not country:
        return "Other"
    return REGION_OF_COUNTRY.get(country.strip(), "Other")


def region_of_exchange(exchange: str | None) -> str | None:
    """Region of a TradingView exchange code (listing venue), or None when unknown."""
    if not exchange:
        return None
    return REGION_OF_EXCHANGE.get(exchange.strip().upper())


def region_for(country: str | None, exchange: str | None) -> str:
    """Region of a company line.

    Rule: a mapped, non-offshore country wins. When the country is an offshore incorporation jurisdiction, NULL or
    unmapped, the listing venue decides (NASDAQ/NYSE -> US, HKEX -> Hong Kong, TSE -> Japan, LSE -> Europe & UK...).
    Only when the exchange is unknown too does it fall back to region_of(country) ('Offshore / Caribbean' or 'Other').
    """
    c = (country or "").strip()
    region = REGION_OF_COUNTRY.get(c)
    if region is not None and c not in OFFSHORE_COUNTRIES:
        return region
    return region_of_exchange(exchange) or region_of(country)


_TEXT_OK = "d.text IS NOT NULL AND trim(d.text) <> ''"

# A company "has an SEC document" when an SEC-sourced documents row with extracted text is attached to any of its
# lines (by security_id or company_key, or through the line's 'sec_cik' identifier -> documents.cik, which covers
# class lines of one CIK with different ISINs such as BF.A / BF.B). Rows without text (failed extraction) do not
# count.
_DOC_OK = "doc.source_id LIKE 'sec%' AND doc.text_path IS NOT NULL"
_OFFICIAL_OK = ("doc.text_path IS NOT NULL AND (doc.source_id LIKE 'sec%' OR doc.source_id IN ("
                + ", ".join(f"'{x}'" for x in OFFICIAL_DOC_SOURCES) + "))")
_NATIVE_JOIN = " OR ".join(
    f"(i.id_type = '{t}' AND " + ("doc.source_id LIKE 'sec%'" if s == "sec_filing_text" else f"doc.source_id = '{s}'")
    + ")" for t, s in _NATIVE_IDS.items())
# Stand-in when the documents table is absent (a store created before it existed, opened read-only).
_NO_DOCS = ("(SELECT NULL::VARCHAR AS security_id, NULL::VARCHAR AS company_key, NULL::VARCHAR AS source_id, "
            "NULL::VARCHAR AS form, NULL::VARCHAR AS text_path, NULL::VARCHAR AS snapshot_id, "
            "NULL::VARCHAR AS cik WHERE false)")
_NO_IDS = ("(SELECT NULL::VARCHAR AS security_id, NULL::VARCHAR AS id_type, NULL::VARCHAR AS id_value "
           "WHERE false)")

_COMPANY_SQL = f"""
WITH u AS (SELECT security_id, company_key, name, country, exchange, market_cap_usd FROM universe),
ttm AS (
    SELECT f.security_id FROM fundamentals_current f
    JOIN (SELECT security_id, max(as_of) AS as_of FROM fundamentals_current
          WHERE metric = 'total_revenue' AND period = 'ttm' GROUP BY 1) x
      ON x.security_id = f.security_id AND x.as_of = f.as_of
    WHERE f.metric = 'total_revenue' AND f.period = 'ttm'
      AND (f.value_usd IS NOT NULL OR f.value_local IS NOT NULL)),
fy AS (
    SELECT security_id, count(DISTINCT fiscal_year) AS n FROM fundamentals_annual
    WHERE metric = 'total_revenue' AND value_local IS NOT NULL GROUP BY 1),
dh AS (
    SELECT u.security_id, d.source_id FROM u JOIN descriptions d ON d.security_id = u.security_id WHERE {_TEXT_OK}
    UNION
    SELECT u.security_id, d.source_id FROM u JOIN descriptions d ON d.company_key = u.company_key WHERE {_TEXT_OK}),
da AS (SELECT security_id, list(DISTINCT source_id ORDER BY source_id) AS sources FROM dh GROUP BY 1),
doch AS (
    SELECT u.security_id, doc.form FROM u JOIN {{docs}} doc ON doc.security_id = u.security_id WHERE {_DOC_OK}
    UNION
    SELECT u.security_id, doc.form FROM u JOIN {{docs}} doc ON doc.company_key = u.company_key WHERE {_DOC_OK}
    UNION
    SELECT u.security_id, doc.form FROM u JOIN {{ids}} i ON i.security_id = u.security_id AND i.id_type = 'sec_cik'
    JOIN {{docs}} doc ON doc.cik = i.id_value WHERE {_DOC_OK}),
dca AS (SELECT security_id, list(DISTINCT coalesce(form, '?') ORDER BY 1) AS forms FROM doch GROUP BY 1),
offh AS (
    SELECT u.security_id, doc.source_id FROM u JOIN {{docs}} doc ON doc.security_id = u.security_id
    WHERE {_OFFICIAL_OK}
    UNION
    SELECT u.security_id, doc.source_id FROM u JOIN {{docs}} doc ON doc.company_key = u.company_key
    WHERE {_OFFICIAL_OK}
    UNION
    SELECT u.security_id, doc.source_id FROM u JOIN {{ids}} i ON i.security_id = u.security_id
    JOIN {{docs}} doc ON doc.cik = i.id_value AND ({_NATIVE_JOIN}) WHERE {_OFFICIAL_OK}),
offa AS (SELECT security_id, list(DISTINCT source_id ORDER BY source_id) AS sources FROM offh GROUP BY 1)
SELECT u.security_id, u.name, u.country, u.exchange, u.market_cap_usd,
       u.security_id IN (SELECT security_id FROM ttm) AS has_ttm,
       coalesce(fy.n, 0) AS n_years,
       coalesce(da.sources, []) AS desc_sources,
       coalesce(dca.forms, []) AS doc_forms,
       coalesce(offa.sources, []) AS official_sources
FROM u LEFT JOIN fy ON fy.security_id = u.security_id LEFT JOIN da ON da.security_id = u.security_id
       LEFT JOIN dca ON dca.security_id = u.security_id LEFT JOIN offa ON offa.security_id = u.security_id
"""

_TIER_SQL = """
WITH u AS (SELECT security_id, company_key, last_seen_snapshot FROM universe),
r AS (
    SELECT security_id, last_seen_snapshot AS snapshot_id FROM u
    UNION ALL
    SELECT m.security_id, m.snapshot_id FROM latest_market m JOIN u ON u.security_id = m.security_id
    UNION ALL
    SELECT f.security_id, f.snapshot_id FROM fundamentals_current f
    JOIN (SELECT security_id, max(as_of) AS as_of FROM fundamentals_current GROUP BY 1) x
      ON x.security_id = f.security_id AND x.as_of = f.as_of
    JOIN u ON u.security_id = f.security_id
    UNION ALL
    SELECT a.security_id, a.snapshot_id FROM fundamentals_annual a JOIN u ON u.security_id = a.security_id
    UNION ALL
    SELECT security_id, snapshot_id FROM (
        SELECT u.security_id, d.source_id, d.snapshot_id FROM u JOIN descriptions d ON d.security_id = u.security_id
        UNION
        SELECT u.security_id, d.source_id, d.snapshot_id FROM u JOIN descriptions d ON d.company_key = u.company_key)
    UNION ALL
    SELECT security_id, snapshot_id FROM (
        SELECT u.security_id, doc.text_path, doc.snapshot_id FROM u JOIN {docs} doc ON doc.security_id = u.security_id
        UNION
        SELECT u.security_id, doc.text_path, doc.snapshot_id FROM u JOIN {docs} doc ON doc.company_key = u.company_key
        UNION
        SELECT u.security_id, doc.text_path, doc.snapshot_id FROM u
        JOIN {ids} i ON i.security_id = u.security_id AND i.id_type = 'sec_cik' JOIN {docs} doc ON doc.cik = i.id_value))
SELECT coalesce(s.license_tier, 'unknown') AS tier, count(DISTINCT r.security_id) AS companies, count(*) AS n_rows
FROM r LEFT JOIN snapshots n ON n.snapshot_id = r.snapshot_id LEFT JOIN sources s ON s.source_id = n.source_id
GROUP BY 1 ORDER BY 1
"""


def _has_table(con, name: str) -> bool:
    return bool(con.execute("SELECT count(*) FROM information_schema.tables "
                            "WHERE table_schema = 'main' AND table_name = ?", [name]).fetchone()[0])


def _ids_relation(con) -> str:
    """'identifiers' when the table exists, else an empty stand-in."""
    return "identifiers" if _has_table(con, "identifiers") else _NO_IDS


def _docs_relation(con) -> str:
    """The documents a screen reads when no top-N lever is on (the deep rows of an on-demand deep fetch, section
    'business_deep', are left out: sources/deep_sections.py), else an empty stand-in (read-only connections never
    create tables)."""
    n = con.execute("SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'main' AND table_name = 'documents'").fetchone()[0]
    return ("(SELECT * FROM documents WHERE section IS DISTINCT FROM 'business_deep')" if n else _NO_DOCS)


def _iso(v: Any) -> str | None:
    return v.isoformat() if v is not None and hasattr(v, "isoformat") else (None if v is None else str(v))


def report(con) -> dict[str, Any]:
    """Coverage of the screenable universe (one row per company). All counts come from non-NULL values only.

    Pure SQL reads over the store; works on a read-only connection. Regions use region_for(country, exchange).
    """
    docs = _docs_relation(con)
    ids = _ids_relation(con)
    rows = con.execute(_COMPANY_SQL.format(docs=docs, ids=ids)).fetchall()
    zero = {m: 0 for m in METRICS}
    by_region: dict[str, dict[str, int]] = {r: dict(zero) for r in REGIONS}
    totals = dict(zero)
    by_source: dict[str, int] = {s: 0 for s in DESCRIPTION_SOURCES}
    by_form: dict[str, int] = {}
    by_official: dict[str, int] = {s: 0 for s in OFFICIAL_DOC_SOURCES}
    by_region_official: dict[str, dict[str, int]] = {}
    gaps_pool: list[tuple[float, str, str | None, str | None, str | None, bool, bool]] = []
    for security_id, name, country, exchange, mcap, has_ttm, n_years, sources, forms, official in rows:
        flags = {
            "companies": True, "market_cap": mcap is not None, "ttm_revenue": bool(has_ttm),
            "revenue_fy3": n_years >= 3, "revenue_fy10": n_years >= 10, "description": bool(sources),
            "sec_document": bool(forms), "official_document": bool(official),
        }
        region = region_for(country, exchange)
        bucket = by_region[region]
        for s in official:
            by_official[s] = by_official.get(s, 0) + 1
            reg = by_region_official.setdefault(region, {})
            reg[s] = reg.get(s, 0) + 1
        for m, ok in flags.items():
            if ok:
                bucket[m] += 1
                totals[m] += 1
        for s in sources:
            by_source[s] = by_source.get(s, 0) + 1
        for f in forms:
            by_form[f] = by_form.get(f, 0) + 1
        if not sources and mcap is not None:
            gaps_pool.append((mcap, security_id, name, country, exchange, bool(forms), bool(official)))
    gaps_pool.sort(key=lambda g: (-g[0], g[1]))
    gaps = [{"security_id": sid, "name": nm, "country": ctry, "region": region_for(ctry, ex), "market_cap_usd": mc,
             "has_sec_document": has_doc, "has_official_document": has_off}
            for mc, sid, nm, ctry, ex, has_doc, has_off in gaps_pool[:GAP_LIMIT]]

    tiers = {t: {"companies": c, "rows": n} for t, c, n in con.execute(_TIER_SQL.format(docs=docs, ids=ids)).fetchall()}
    market_as_of = con.execute("SELECT max(market_as_of) FROM universe").fetchone()[0]
    snaps = con.execute("SELECT source_id, max(fetched_at), count(*) FROM snapshots GROUP BY 1 ORDER BY 1").fetchall()
    return {
        "totals": totals,
        "by_region": {r: v for r, v in by_region.items() if v["companies"]},
        "description_by_source": dict(sorted(by_source.items())),
        "sec_document_by_form": dict(sorted(by_form.items())),
        "official_document_by_source": dict(sorted(by_official.items())),
        "official_document_by_region_source": {r: dict(sorted(by_region_official[r].items()))
                                               for r in REGIONS if r in by_region_official},
        "license_tiers": tiers,
        "freshness": {
            "market_as_of": _iso(market_as_of),
            "latest_snapshot": {s: {"fetched_at": _iso(t), "snapshots": n} for s, t, n in snaps},
        },
        "gaps": {"top_market_cap_without_description": gaps},
    }


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    fmt = lambda cells: "  ".join(c.ljust(w) if i == 0 else c.rjust(w) for i, (c, w) in enumerate(zip(cells, widths)))
    return "\n".join([fmt(headers), fmt(["-" * w for w in widths]), *(fmt(r) for r in rows)])


def _pct(n: int, total: int) -> str:
    return f"{n} ({100 * n / total:.0f}%)" if total else str(n)


def _money(v: float | None) -> str:
    if v is None:
        return "-"
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
        if abs(v) >= div:
            return f"{v / div:.1f}{unit}"
    return f"{v:,.0f}"


def format_report(d: dict[str, Any]) -> str:
    """Render report() output as plain-text tables."""
    headers = ["region", "companies", "mcap", "ttm_rev", ">=3y_rev", ">=10y_rev", "desc", "sec_doc", "official_doc"]
    rows = []
    for name, v in [*d["by_region"].items(), ("TOTAL", d["totals"])]:
        c = v["companies"]
        rows.append([name, str(c), *(_pct(v[m], c) for m in METRICS[1:])])
    out = ["Coverage (one row per company, from the universe view)", _table(headers, rows), ""]

    out.append("Descriptions by source")
    src = [[s, str(n)] for s, n in d["description_by_source"].items()]
    out += [_table(["source", "companies"], src) if src else "  (none)", ""]

    out.append("Companies with an SEC document (text extracted), by form")
    forms = [[f, str(n)] for f, n in d.get("sec_document_by_form", {}).items()]
    out += [_table(["form", "companies"], forms) if forms else "  (none)", ""]

    out.append("Companies with official annual-report text, by region and source")
    srcs = list(d.get("official_document_by_source", {}))
    reg = d.get("official_document_by_region_source", {})
    orows = [[r, *(str(v.get(s, 0)) for s in srcs)] for r, v in reg.items()]
    orows.append(["TOTAL", *(str(d["official_document_by_source"].get(s, 0)) for s in srcs)])
    out += [_table(["region", *srcs], orows) if srcs else "  (none)", ""]

    out.append("Licence tiers of underlying snapshots")
    tiers = [[t, str(v["companies"]), str(v["rows"])] for t, v in d["license_tiers"].items()]
    out += [_table(["tier", "companies", "rows"], tiers) if tiers else "  (none)", ""]

    f = d["freshness"]
    out.append(f"Freshness: latest market as_of = {f['market_as_of'] or '-'}")
    snaps = [[s, v["fetched_at"] or "-", str(v["snapshots"])] for s, v in f["latest_snapshot"].items()]
    out += [_table(["source", "latest_snapshot", "snapshots"], snaps) if snaps else "  (no snapshots)", ""]

    gaps = d["gaps"]["top_market_cap_without_description"]
    out.append(f"Gap: top {GAP_LIMIT} companies by market cap without a description")
    g = [[x["security_id"], (x["name"] or "")[:40], x["region"], _money(x["market_cap_usd"])] for x in gaps]
    out.append(_table(["security_id", "name", "region", "mcap_usd"], g) if g else "  (none)")
    return "\n".join(out)
