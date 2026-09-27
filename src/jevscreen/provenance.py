"""Every stored row points to a snapshot, every snapshot to a source, every source to a licence tier.

The tier decides what may ever leave this machine. Everything is usable locally for personal research; only
OFFICIAL_OPEN data (and outputs derived solely from it, plus the allowlisted open-pack sources) may be published.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class LicenseTier(str, Enum):
    OFFICIAL_OPEN = "official-open"        # official source, licence permits redistribution (e.g. SEC bulk, TWSE OpenAPI)
    OFFICIAL_PRIVATE = "official-private"  # official source, personal use only (e.g. exchange downloads)
    GRAY_PRIVATE = "gray-private"          # public but terms-restricted endpoint; personal use only, never publish


@dataclass(frozen=True)
class Source:
    source_id: str
    name: str
    tier: LicenseTier
    terms_url: str
    notes: str = ""


SOURCES: dict[str, Source] = {
    "tradingview_scanner": Source(
        "tradingview_scanner", "TradingView screener endpoint (scanner.tradingview.com)", LicenseTier.GRAY_PRIVATE,
        "https://www.tradingview.com/policies/",
        "Terms s.3 forbid non-display use incl. algorithmic decision-making. Personal use only; never redistribute."),
    "tradingview_profile": Source(
        "tradingview_profile", "TradingView symbol page JSON-LD business description", LicenseTier.GRAY_PRIVATE,
        "https://www.tradingview.com/policies/",
        "Robots allow /symbols/ for generic agents; terms restrict reuse. Personal use only."),
    "financedatabase_local": Source(
        "financedatabase_local", "FinanceDatabase snapshot (summaries are Yahoo longBusinessSummary text)",
        LicenseTier.GRAY_PRIVATE, "https://github.com/JerBouma/FinanceDatabase",
        "Repo is MIT but the summary text originates from Yahoo; treat as personal use only."),
    "sec_tickers": Source(
        "sec_tickers", "SEC company_tickers_exchange.json and submissions API (data.sec.gov)", LicenseTier.OFFICIAL_OPEN,
        "https://www.sec.gov/about/privacy-information",
        "SEC-compiled public information; may be copied or redistributed. Requires a declared User-Agent, <=10 req/s total."),
    "sec_filing_text": Source(
        "sec_filing_text", "SEC EDGAR annual report sections (10-K Item 1, 20-F Item 4)", LicenseTier.OFFICIAL_PRIVATE,
        "https://www.sec.gov/about/privacy-information",
        "Issuer-authored narrative is not a government work; keep verbatim text local. Derived labels may be published."),
    "cninfo_annual_report": Source(
        "cninfo_annual_report", "CNINFO (巨潮资讯) A-share annual report / summary PDFs", LicenseTier.OFFICIAL_PRIVATE,
        "http://www.cninfo.com.cn/",
        "Exchange-disclosed documents; exchanges assert exclusive rights over securities information. Personal use only; keep text local."),
    "edinet_yuho": Source(
        "edinet_yuho", "EDINET API v2 annual securities reports (有価証券報告書, 事業の内容)", LicenseTier.OFFICIAL_PRIVATE,
        "https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/WZEK0030.html",
        "Site content under PDL 1.0 with attribution; only the code list and 事業の内容 ship in the open pack "
        "(jevscreen.pack allowlist), the rest stays local. Requires the user's own free API key."),
    "dart_business_report": Source(
        "dart_business_report", "OpenDART business reports (사업보고서, 사업의 개요)", LicenseTier.OFFICIAL_PRIVATE,
        "https://opendart.fss.or.kr/intro/terms.do",
        "No explicit redistribution grant; personal use only. Requires the user's own free API key (~20,000 calls/day). "
        "The default web mode reads the section text from the DART website (dart.fss.or.kr viewer, its own terms): "
        "same rule, keep it local."),
    "bse_annual_report": Source(
        "bse_annual_report", "BSE India annual report PDFs (MD&A / company overview pages)", LicenseTier.OFFICIAL_PRIVATE,
        "https://www.bseindia.com/",
        "Exchange-hosted issuer filings; no redistribution grant found and no BSE terms-of-use page located "
        "(checked 2026-09-27). Personal use only; keep text local."),
}

# Taiwan (sources/mops.py). Added apart from the literal above.
SOURCES["mops_annual_report"] = Source(
    "mops_annual_report", "MOPS / TWSE e-documents: annual reports (股東會年報, 營運概況 / 業務內容), doc.twse.com.tw",
    LicenseTier.OFFICIAL_PRIVATE, "https://mops.twse.com.tw/mops/#/web/home",
    "Issuer-authored reports served by TWSE; no redistribution grant: personal use, keep text local. doc.twse.com.tw "
    "robots.txt disallows all crawlers (2026-09-27): fetch on demand or slowly (>= 1.5 s), never in bulk bursts.")
SOURCES["mops_basic"] = Source(
    "mops_basic", "MOPS basic data (公司基本資料, 主要經營業務) via mops.twse.com.tw/mops/api/t05st03",
    LicenseTier.OFFICIAL_PRIVATE, "https://mops.twse.com.tw/mops/#/web/home",
    "TWSE-operated site, no open-data licence for this field (the open TWSE/TPEx company datasets lack it). "
    "Personal use only.")


def tier_of(source_id: str) -> LicenseTier:
    return SOURCES[source_id].tier
