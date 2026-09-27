"""BSE (Bombay Stock Exchange) adapter: Indian universe lines -> BSE scrip code -> latest annual report PDF -> the
business overview pages (Management Discussion and Analysis, plus the report's own 'About the Company' page).

Source (provenance.py): 'bse_annual_report' (official-private, personal use). The section text is kept in local files
under <home>/docs/bse/<scrip code>/ and never shipped; `documents` holds metadata only, `descriptions` a short
excerpt.

Endpoints (probed live 2026-09-26/27 UTC, ~30 requests in all, no block, no challenge; request headers Referer /
Origin https://www.bseindia.com, the default jev-screen User-Agent):
- GET https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w?Group=&Scripcode=&industry=&segment=Equity
  &status=Active -> JSON list of {SCRIP_CD, Scrip_Name, Status, GROUP, ISIN_NUMBER, scrip_id, Segment, Issuer_Name,
  Mktcap, ...} (5,048 rows, 1.77 MB, 4.2 s; Segment 'Equity' plus a few 'PreferenceShares' / '' rights lines).
- GET https://api.bseindia.com/BseIndiaAPI/api/AnnualReport_New/w?scripcode=<code> -> {'Table': [{Scripcode, Year,
  PDFDownload, Fld_AuthoriseDate, status 'New'|'Revised', revised_date_time, Fld_ResubReason, ...}]} (4-12 KB,
  0.5-1.4 s). 'Year' is BSE's fiscal-year label (RIL 'Integrated Annual Report for the financial year 2025-26' is
  Year 2026). PDFDownload URLs may carry a stray backslash ('AttachHis/\\b55b...pdf'), which is dropped.
- GET https://www.bseindia.com/xml-data/corpfiling/AttachHis/<uuid>.pdf (older: /bseplus/AnnualReport/<code>/...)
  -> the whole annual report as filed (often notice + report + financial statements in one file: 1.1-17.4 MB,
  61-678 pages, 0.5-4.7 s). Akamai-served, Accept-Ranges: bytes, 'Linearized' PDFs; no robots.txt (the SPA answers).

Why the whole PDF: nothing smaller carries the company's own words. The exchange's scrip data has no description;
BRSR (Business Responsibility and Sustainability Report) XBRL exists only for the top 1,000 companies and holds a
one-line 'description of main activity'. Range requests would allow reading single pages of a linearized PDF, but a
page needs its content streams, fonts and ToUnicode maps: tens of ranged requests at >= 1 s each, slower than one
5 s download. So: one listing request + one PDF per company, and only the needed page range is parsed.

Rules implemented here:
- Universe lines on 'BSE' / 'NSE' (TradingView: BSE = Bombay; a CN ISIN on 'BSE' is Beijing and belongs to
  cninfo.py) with an Indian (IN...) or missing ISIN are mapped to Equity scrip rows by ISIN (method 'isin'); a BSE
  line without an ISIN match falls back to its symbol = scrip_id ('scrip_id') or a numeric symbol = SCRIP_CD
  ('scrip_code'). NSE-only companies (NSE Emerge SME etc.) have no BSE scrip and stay unmapped. identifiers
  id_type 'bse_scrip_code'. Lines sharing a scrip code are fetched once.
- The scrip list saved by a run less than SCRIP_LIST_REUSE_DAYS days ago is reused (no request); a downloaded list
  with fewer than MIN_SCRIP_ROWS rows is suspect and stops the run before any identifier changes.
- Latest report (select_latest): the highest 'Year' wins; within a year a 'Revised' row beats the original, then the
  newest revised / authorise time, then the URL. Only PDFs on www.bseindia.com are fetched. 'stale:<year>' is noted
  when the year is behind the newest one that must be out under a 31 March fiscal year (AGM deadline 30 September)
  and the filing is over STALE_MIN_AGE_DAYS old (June / September / December year-ends are not flagged early).
- Before any request, a company whose stored section (current EXTRACTOR_VERSION) already has the newest possible
  year label (this year from April), or was filed less than CURRENT_MAX_AGE_DAYS ago whatever its label (non-March
  year-ends), is 'skipped_current' (unless refresh). A report already processed (documents row
  with the same URL) is not downloaded again unless refresh.
- Business section (extract_section): the PDF outline (bookmarks) is used when it names 'Management Discussion and
  Analysis' (the page is verified within +-2 pages, else ignored); otherwise pages are scanned for the MD&A heading
  among a page's head blocks: the first HEAD_BLOCKS text blocks plus every block in the top HEAD_TOP_FRAC of the
  page (designed reports draw the heading last in the content stream; plain ones start the chapter mid-page inside
  the directors' report). Headings split over blocks and 'Annexure IV' prefixes count; contents pages and directors'
  report items pointing to the chapter ('forms part of', 'is included in / part of this Annual Report', 'is enclosed',
  'furnished separately', 'as per Annexure C', 'as stipulated under') do not; section dividers listing 2+ other
  chapters are tried last, and a heading page whose range holds under MIN_SECTION_CHARS of text gives way to the
  next one (up to MDA_MAX_CANDIDATES). On the start page, blocks
  before the heading in stream order and above it on the page (the previous chapter) are dropped. The MD&A ends at
  the next outline entry or at the next statutory heading (Corporate Governance, BRSR, Auditor's report,
  Annexure, ...), at most MDA_MAX_PAGES pages; a block already printed on an earlier MD&A page (a section tab such
  as 'Financial Statements' / "Board's Report" / 'Annexure B') is not an end heading. Within it, a business
  sub-heading ('Company Overview', 'Business Overview', 'Our Business', 'Segment-wise performance', ...) found after
  the first MDA_LEAD_CHARS chars is moved to the front (large companies open MD&A with the economy). A front-matter
  'About the Company' / 'Who We Are' / 'Company Overview' page (outline or heading) before the MD&A is put first
  (OVERVIEW_MAX_CHARS); pages about the report, the chairman / board, ESG / sustainability or 'the year', pages of
  the directors' report and a heading-scan 'Business Overview' do not count, and a page with under
  MIN_SECTION_CHARS // 2 of text gives way to the next. Without an MD&A: the overview alone ('overview_only'), else the financial statements'
  'Company information' / 'Corporate information' note ('fallback:corporate_information': Ind AS note 1 says what the
  company does), else the directors' report 'State of the Company's affairs' / 'Nature of business' paragraph
  ('fallback:directors_report'), each needing FALLBACK_MIN_CHARS and business words; else 'no_section'.
  Blocks are PyMuPDF text blocks; blocks without enough letters (tables, page numbers) and blocks repeated on 3+
  pages (running headers / footers) are dropped. Only the pages needed are parsed (note 'pages:<read>/<total>').
  PDFs without a text layer fail as 'no_text_layer' (no OCR).
- PDFs are capped at MAX_PDF_BYTES (a bigger file: 'too_large', recorded, not retried under this extractor) with a
  PDF_DEADLINE_S wall-clock deadline. Soft blocks raise http.Blocked at the first occurrence (the client's halt set
  at once): a redirect of the listing API ('redirect_refusal'; BSE sends refused API calls to a members page), an
  HTML page instead of the listing JSON ('non_json_page') and an HTML page instead of a PDF ('non_pdf_body'), unless
  is_site_home_page() recognises the site's own page by its <title> (the SPA answers a dead link with its home page:
  crawl_state 'error' 'pdf_html_page'). Other non-PDF bytes are 'pdf_not_pdf', broken JSON 'list_bad_json'.
  http.challenge_reason only knows Cloudflare signatures and BSE is Akamai-served: an Akamai denial arrives as a
  403 (a block) or as one of the pages above.
- http.Blocked (403 / 429 / challenge / the soft blocks above) stops the whole run at once: the cooldown marker
  (guard.mark_blocked, command 'sync-bse') is written before any DB write, in-flight PDFs drain (bounded), the batch
  is flushed. 5 consecutive errors stop it; Ctrl-C / SIGTERM halts, drains, flushes and records 'interrupted'.
- Rates: one http.Client; the limiter is per host (api.bseindia.com and www.bseindia.com, each >= 1.0 s between
  request starts). Listing requests run on the main thread; PDFs are fetched + parsed by `workers` daemon threads
  (default DEFAULT_WORKERS, 1..MAX_WORKERS; PDF parsing serialised by cninfo's process-wide PyMuPDF lock). No DB
  connection is held during network I/O: every DB touch is a short store.session() per batch.
- fetch_company(client, code) is the single-company path without any database (listing + PDF + extraction).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import http.client
import json
import queue as queue_mod
import re
import sys
import threading
import time
import urllib.parse
import uuid
from concurrent.futures import FIRST_COMPLETED, Future
from concurrent.futures import wait as futures_wait
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .. import guard, store
from ..config import Config
from ..http import Blocked, Client, Halted
from . import cninfo

SOURCE_ID = "bse_annual_report"
COMMAND = "sync-bse"
SCRIP_LIST_URL = ("https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w?Group=&Scripcode=&industry="
                  "&segment=Equity&status=Active")
AR_LIST_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnualReport_New/w?scripcode={code}"
SITE = "https://www.bseindia.com"
PDF_HOSTS = frozenset({"www.bseindia.com"})
API_HEADERS = {"Referer": SITE + "/", "Origin": SITE, "Accept": "application/json, text/plain, */*"}
PDF_HEADERS = {"Referer": SITE + "/", "Accept": "application/pdf,*/*"}
DEFAULT_MIN_INTERVAL_S = 1.0        # per host: api.bseindia.com and www.bseindia.com
DEFAULT_WORKERS = 2                 # PDF fetch + extract threads
MAX_WORKERS = 3
ID_TYPE = "bse_scrip_code"
EXTRACTOR_VERSION = "bse-v2"           # v2: MD&A tabs / pointers / dividers; v1 failures retried
SECTION = "business"
FORM = "annual_report"
MAX_PDF_BYTES = 64 * 1024 * 1024
PDF_DEADLINE_S = 300.0
LIST_DEADLINE_S = 60.0
SCRIP_LIST_DEADLINE_S = 120.0
SCRIP_LIST_REUSE_DAYS = 3
MIN_SCRIP_ROWS = 3_000              # live 2026-09-27: 5,048
MIN_SECTION_CHARS = 300
MAX_SECTION_CHARS = 40_000
OVERVIEW_MAX_CHARS = 5_000
OVERVIEW_MAX_PAGES = 3
MDA_LEAD_CHARS = 1_500              # a business sub-heading further into the MD&A is moved to the front
MDA_LEAD_KEEP_CHARS = 4_000         # ... and this much of the MD&A opening is kept after it
MDA_MAX_PAGES = 25
MDA_MAX_CANDIDATES = 6              # MD&A heading pages tried when a range holds too little text (dividers)
FALLBACK_MAX_CHARS = 3_000
FALLBACK_MIN_CHARS = 120            # a one-paragraph 'Company information' note is still worth keeping
HEAD_BLOCKS = 10                    # headings: a page's first blocks (text without positions) ...
HEAD_TOP_FRAC = 0.3                 # ... or the blocks starting in the top 30% of the page (PyMuPDF)
RUNNING_MIN_PAGES = 3               # a short block on this many pages of the range is a running header / footer
TEXT_LAYER_MIN_CHARS = 200          # a page with fewer chars has no usable text
TEXT_LAYER_PROBE_PAGES = 40
SHORT_DESC_CHARS = 1_200
MAX_CONSECUTIVE_ERRORS = 5
STALE_MIN_AGE_DAYS = 365            # 'stale' also needs the report to be this old (non-March fiscal years)
CURRENT_MAX_AGE_DAYS = 270          # a stored report filed this recently is current whatever its year label
FLUSH_WAIT_S = 120.0
FINAL_WAIT_S = 600.0
RESULT_POLL_S = 0.5
DRAIN_MAX_S = 60.0
IN_VENUES = frozenset({"BSE", "NSE"})
CRAWL_STATUSES = ("ok", "no_annual_report", "extract_failed", "error", "blocked")
_NOTE_FY_RE = re.compile(r"(?:^|;)fy_label:(\d{4})(?:;|$)")


# =========================================================================== universe mapping


def parse_scrip_list(payload: Any) -> list[dict]:
    """Rows of ListofScripData as dicts (bytes / str / list accepted). Raises ValueError when it is not a list."""
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8-sig")
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, list):
        raise ValueError("scrip list is not a JSON list")
    out = []
    for r in payload:
        if not isinstance(r, Mapping):
            continue
        code = str(r.get("SCRIP_CD") or "").strip()
        if not code.isdigit():
            continue
        out.append({"code": code, "scrip_id": str(r.get("scrip_id") or "").strip().upper(),
                    "isin": str(r.get("ISIN_NUMBER") or "").strip().upper(),
                    "name": r.get("Issuer_Name") or r.get("Scrip_Name"),
                    "segment": str(r.get("Segment") or "").strip(), "status": str(r.get("Status") or "").strip(),
                    "group": str(r.get("GROUP") or "").strip()})
    return out


def is_in_line(sec: Mapping[str, Any]) -> bool:
    """An Indian listing line: exchange BSE / NSE and an IN... ISIN (or none). 'BSE' with a CN ISIN is Beijing."""
    ex = str(sec.get("exchange") or "").strip().upper()
    if ex not in IN_VENUES:
        return False
    isin = str(sec.get("isin") or "").strip().upper()
    return not isin or isin.startswith("IN")


def map_securities_to_scrip(securities: Sequence[Mapping[str, Any]], scrip_rows: Iterable[Mapping[str, Any]], *,
                            report: dict | None = None) -> list[tuple[str, str, str]]:
    """[(security_id, scrip_code, method)] for Indian lines. Equity + Active rows only; an ISIN carried by two scrip
    rows is ambiguous and not mapped. report (optional) receives 'ambiguous', 'unmatched', 'unmatched_by_exchange'."""
    eq = [r for r in scrip_rows if r.get("segment", "Equity") == "Equity" and r.get("status", "Active") == "Active"]
    by_isin: dict[str, list[str]] = {}
    for r in eq:
        if len(r.get("isin") or "") == 12:
            by_isin.setdefault(r["isin"], []).append(r["code"])
    by_sid = {r["scrip_id"]: r["code"] for r in eq if r.get("scrip_id")}
    codes = {r["code"] for r in eq}
    out: list[tuple[str, str, str]] = []
    ambiguous: list[str] = []
    unmatched: dict[str, int] = {}
    for sec in securities:
        if not is_in_line(sec):
            continue
        sid = str(sec["security_id"])
        ex = str(sec.get("exchange") or "").strip().upper()
        isin = str(sec.get("isin") or "").strip().upper()
        sym = str(sec.get("symbol") or sid.rsplit(":", 1)[-1]).strip().upper()
        hits = sorted(set(by_isin.get(isin, []))) if isin else []
        if len(hits) == 1:
            out.append((sid, hits[0], "isin"))
            continue
        if len(hits) > 1:
            ambiguous.append(sid)
            continue
        if ex == "BSE" and sym in by_sid:
            out.append((sid, by_sid[sym], "scrip_id"))
        elif ex == "BSE" and sym.isdigit() and sym in codes:
            out.append((sid, sym, "scrip_code"))
        else:
            unmatched[ex] = unmatched.get(ex, 0) + 1
    if report is not None:
        report["ambiguous"] = ambiguous
        report["unmatched"] = sum(unmatched.values())
        report["unmatched_by_exchange"] = unmatched
    return out


# =========================================================================== annual report listing


def pdf_url(raw: Any) -> str | None:
    """Normalised PDF URL on www.bseindia.com, or None (empty / foreign host / not http). Stray backslashes dropped."""
    s = str(raw or "").strip().replace("\\", "")
    if not s:
        return None
    if s.startswith("/"):
        s = SITE + s
    p = urllib.parse.urlsplit(s)
    if p.scheme not in ("http", "https") or p.netloc.lower() not in PDF_HOSTS or not p.path:
        return None
    path = re.sub(r"/{2,}", "/", p.path)
    return urllib.parse.urlunsplit(("https", p.netloc.lower(), path, p.query, ""))


def _parse_ts(v: Any) -> dt.datetime | None:
    s = str(v or "").strip()
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s[:19])
    except ValueError:
        try:
            return dt.datetime.fromisoformat(s[:10])
        except ValueError:
            return None


def parse_ar_listing(payload: Any) -> list[dict]:
    """Normalised rows of AnnualReport_New: {year, url, accession, filing_date, revised, ts, reason}. Rows without a
    plausible year or a www.bseindia.com PDF URL are dropped. Raises ValueError for a body that is not the object."""
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8-sig")
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, Mapping):
        raise ValueError("annual report listing is not a JSON object")
    table = payload.get("Table")
    if table is None:
        table = []
    if not isinstance(table, list):
        raise ValueError("annual report listing: 'Table' is not a list")
    out = []
    for r in table:
        if not isinstance(r, Mapping):
            continue
        ys = str(r.get("Year") or "").strip()
        if not re.fullmatch(r"(?:19|20)\d{2}", ys):
            continue
        url = pdf_url(r.get("PDFDownload"))
        if url is None:
            continue
        revised = str(r.get("status") or "").strip().lower() == "revised" or bool(r.get("Fld_ReSubmit"))
        rts, ats = _parse_ts(r.get("revised_date_time")), _parse_ts(r.get("Fld_AuthoriseDate"))
        ts = (rts or ats) if revised else (ats or rts)
        stem = Path(urllib.parse.urlsplit(url).path).stem
        out.append({"year": int(ys), "url": url, "accession": stem, "filing_date": ts.date() if ts else None,
                    "revised": revised, "ts": ts, "reason": str(r.get("Fld_ResubReason") or "").strip() or None})
    return out


def select_latest(rows: Sequence[Mapping[str, Any]]) -> dict | None:
    """The report to read: newest year label, then a revision over the original, then the newest time, then URL."""
    if not rows:
        return None
    top = max(r["year"] for r in rows)
    cands = [r for r in rows if r["year"] == top]
    return dict(max(cands, key=lambda r: (bool(r["revised"]), r["ts"] or dt.datetime.min, r["url"])))


def newest_possible_label(today: dt.date) -> int:
    """The newest BSE year label a report can have: fiscal years end 31 March, so this year's label from April."""
    return today.year if today.month >= 4 else today.year - 1


def expected_latest_label(today: dt.date) -> int:
    """The year label that must be out: the AGM (and the report) is due by 30 September."""
    return today.year if today.month >= 10 else today.year - 1


def _today() -> dt.date:
    return dt.date.today()


# =========================================================================== extraction

_WS_RE = re.compile(r"[ \t\r\f\v  -​ 　]+")
_LETTER_RE = re.compile(r"[A-Za-z]")
_APOS = str.maketrans({"’": "'", "‘": "'", "`": "'", "–": "-", "—": "-", "“": '"', "”": '"'})
_MDA_CORE = r"management\s*'?\s*s?\s+discussions?\s*(?:and|&)\s*analysis"
_MDA_RX = re.compile(
    r"^(?:annexure[\s\-:.]*[a-z0-9]{0,5}[\s\-:.)]*(?:to\s+the\s+(?:directors'?|board'?s)\s+report[\s\-:.]*)?)?"
    + _MDA_CORE +
    r"(?:\s+report)?(?:\s*\((?:md\s*&\s*a|mda|mdar)\))?(?:\s*[-:]\s*[a-z &,']{0,40})?\s*[:.\-]?$", re.I)
_MDA_TITLE_RX = re.compile(_MDA_CORE, re.I)
_NOT_THE_COMPANY = (r"(?:(?:the|this|our)\s+)?(?:(?:integrated|annual|sustainability|esg)\s+)*report"
                    r"|(?:(?:the|our)\s+)?(?:chairman|chairperson|chair|board|directors?|management|leadership|founders?"
                    r"|promoters?|ceo|md)\b|(?:(?:the|our|this)\s+)?(?:esg|sustainability|integrated|(?:the\s+)?year|fy"
                    r"|kpis?|key|information|agm|meeting|notice|financials?|performance|highlights|shareholders?"
                    r"|operational|numbers)\b")
_OVERVIEW_RX = re.compile(
    r"^(?:about\s+(?!" + _NOT_THE_COMPANY + r")(?:the\s+company|us|[a-z0-9&.' ]{2,40})"
    r"|who\s+we\s+are|company\s+(?:overview|profile|at\s+a\s+glance)|corporate\s+profile"
    r"|(?!" + _NOT_THE_COMPANY + r")(?:[a-z0-9&.' ]{2,40}\s+)?"
    r"at\s+a\s+glance|business\s+overview|our\s+company"
    r"|overview\s+of\s+the\s+company)\s*[:.]?$", re.I)
_BOARD_REPORT_RX = re.compile(r"^(?:the\s+)?(?:directors'?|board'?s)\s+report$", re.I)
_BUSINESS_RX = re.compile(
    r"^(?:\(?(?:[a-z]|\d{1,2}|[ivx]{1,4})[.)]\s*)?(?:(?:company|business|corporate)\s+(?:overview|profile)"
    r"|overview\s+of\s+(?:the\s+)?(?:company'?s?\s+)?(?:company|business|operations)|about\s+(?:the\s+company|us)"
    r"|our\s+business(?:es)?|business\s+(?:segments?|operations|model|verticals)"
    r"|(?:business|operational|operating|segment(?:al)?)\s+(?:performance\s+)?(?:review|overview|performance|highlights)"
    r"|segment[\s-]*wise(?:\s+(?:or|/)\s+product[\s-]*wise)?\s+performance|operations?\s+review"
    r"|(?:the\s+)?company'?s\s+(?:business|operations)|review\s+of\s+(?:business|operations))\s*[:.\-]?$", re.I)
_END_RX = re.compile(
    r"^(?:annexure\b.*|(?:report\s+on\s+)?corporate\s+governance(?:\s+report)?|business\s+responsibility.*"
    r"|independent\s+auditors?'?s?'?\s+report.*|auditors?'?s?'?\s+report.*|secretarial\s+audit\s+report.*"
    r"|(?:the\s+)?(?:directors'?|board'?s)\s+report|notice(?:\s+of\s+.*)?|(?:standalone|consolidated)\s+financial"
    r"\s+statements|financial\s+statements|balance\s+sheet.*|form\s+(?:no\.?\s*)?(?:mr-?3|aoc-?\d).*"
    r"|ceo\s*/?\s*cfo\s+certification.*|certificate\s+(?:of|on|under)\s+.*)$", re.I)
_TOC_TITLE_RX = re.compile(r"^(?:table\s+of\s+)?contents?|index|inside\s+this\s+report|what'?s\s+inside$", re.I)
_SECTION_NAME_RX = re.compile(
    r"notice|directors'?\s+report|board'?s\s+report|corporate\s+governance|" + _MDA_CORE +
    r"|auditor|balance\s+sheet|financial\s+statements|business\s+responsibility|profit\s*(?:and|&)\s*loss"
    r"|cash\s+flow", re.I)
_DIRECTORS_RX = re.compile(
    r"^(?:\(?(?:[a-z]|\d{1,2}|[ivx]{1,4})[.)]\s*)?(?:state\s+of\s+(?:the\s+)?company'?s\s+affairs"
    r"|nature\s+of\s+(?:the\s+)?business|business\s+overview|company\s+overview|overview\s+of\s+(?:the\s+)?business"
    r"|review\s+of\s+operations)\s*[:.\-]?$", re.I)
_CORP_INFO_RX = re.compile(
    r"^(?:note\s*(?:no\.?)?\s*\d{1,2}\s*[:.\-]?\s*)?(?:\(?(?:[a-z]|\d{1,2}|[ivx]{1,4})[.)]\s*)?"
    r"(?:(?:corporate|company|general)\s+(?:information|overview|background)|nature\s+of\s+operations"
    r"|background|reporting\s+entity)\s*[:.\-]?$", re.I)
_BUSINESSY_RX = re.compile(r"engaged|business|objects?\b|objective|principal|activit|manufactur|provid|services"
                           r"|products|trading", re.I)


def normalise_block(text: str) -> str:
    """One PDF text block as one line: dehyphenated line breaks, whitespace collapsed, curly quotes straightened."""
    s = str(text or "").replace("­", "")
    # a line-end hyphen: kept after a short word part ('value-/added', 'end-to-/end'), dropped inside a longer
    # word ('manufac-/turing')
    s = re.sub(r"(?<![A-Za-z])([A-Za-z]{1,5})-\s*\n\s*(?=[a-z])", r"\1-", s)
    s = re.sub(r"(?<=[a-z])-\s*\n\s*(?=[a-z])", "", s)
    s = s.replace("\n", " ")
    return _WS_RE.sub(" ", s).strip()


def _key(block: str) -> str:
    return _WS_RE.sub(" ", block.translate(_APOS)).strip().lower()


def _run_key(block: str) -> str:
    """Running-header key: digits masked, so 'Annual Report 2025-26 12 13' repeats across pages."""
    return re.sub(r"\d+", "#", _key(block))


def is_texty(block: str) -> bool:
    """Enough letters to be prose or a heading (drops tables of numbers, page numbers, lone symbols)."""
    s = block.replace(" ", "")
    if not s:
        return False
    return len(_LETTER_RE.findall(s)) >= max(2, 0.4 * len(s))


def is_mda_heading(block: str) -> bool:
    k = _key(block)
    return len(k) <= 140 and bool(_MDA_RX.match(k))


_REFERENCE_RX = re.compile(
    r"form(?:s|ing)?\s+(?:an?\s+)?(?:integral\s+)?part|attached|annexed|appended|enclosed|furnished|separate\s+section"
    r"|(?:is|are|was|were|being|been)\s+(?:an?\s+)?(?:integral\s+)?part\s+of\s+(?:this|the)\s+(?:annual\s+|integrated\s+)*"
    r"report|(?:is|are|was|were|being|been)\s+(?:also\s+)?(?:given|provided|presented|set\s+out|included|covered|detailed"
    r"|incorporated|reproduced|disclosed|placed)\b|given\s+(?:in|as)|(?:as|in|at|vide)\s+(?:per\s+)?annexure"
    r"|as\s+required\s+under|as\s+stipulated|stipulated\s+under", re.I)


def mda_heading_pos(head: Sequence[str]) -> int | None:
    """Position in `head` (Pages.head) where an MD&A heading starts (also split over 2-4 blocks), unless it is a
    directors' report item pointing to the chapter ('... forms part of this report', 'is attached as Annexure B',
    'is included in / is part of this Annual Report', 'is enclosed / furnished separately', '... as stipulated
    under ...'); None when there is none."""
    for i in range(len(head)):
        for j in range(i + 1, min(len(head), i + 4) + 1):
            if is_mda_heading(" ".join(head[i:j])):
                nxt = " ".join(head[j:j + 2])[:400]
                if _REFERENCE_RX.search(nxt):
                    break
                return i
    return None


def page_has_mda_heading(head: Sequence[str]) -> bool:
    """The page opens the MD&A chapter (see mda_heading_pos)."""
    return mda_heading_pos(head) is not None


def is_toc_page(head: Sequence[str], blocks: Sequence[str]) -> bool:
    """A contents page: a 'Contents' / 'Index' heading among the head blocks, or several section names in short
    blocks anywhere on the page."""
    if any(_TOC_TITLE_RX.fullmatch(_key(b)) for b in head):
        return True
    n = sum(1 for b in blocks if len(b) <= 90 and _SECTION_NAME_RX.search(_key(b)))
    return n >= 4


def page_end_heading(head: Sequence[str], ignore: set[str] | frozenset = frozenset()) -> str | None:
    """The statutory section heading that ends the MD&A, if the page starts one (head blocks). Blocks whose
    running-header key (_run_key) is in `ignore` do not count: they already appeared on the MD&A's own pages, so they
    are running headers / section tabs ('Financial Statements', "Board's Report", 'Annexure B'), not a new chapter."""
    for b in head:
        k = _key(b)
        if len(k) <= 120 and _END_RX.match(k) and not _MDA_TITLE_RX.search(k) and _run_key(b) not in ignore:
            return b
    return None


def _short_keys(blocks: Iterable[str]) -> set[str]:
    return {_run_key(b) for b in blocks if len(b) <= 100}


class Pages:
    """Lazy per-page text blocks (normalised strings, content-stream order) of one document, plus its outline.

    `get(i)` returns the page's raw blocks as strings, or as (text, y) pairs where y is the block's top edge as a
    fraction of the page height. Page headings are looked for in head(i): the first HEAD_BLOCKS blocks plus, with
    positions, every block in the top HEAD_TOP_FRAC of the page (designed reports often draw the heading last in
    the content stream; plain ones start a chapter mid-page, in stream order)."""

    def __init__(self, n: int, get: Callable[[int], list[Any]], toc: Sequence[Sequence[Any]] | None = None):
        self.n = n
        self._get = get
        self._cache: dict[int, list[str]] = {}
        self._heads: dict[int, list[int]] = {}
        self._ys: dict[int, list[float | None]] = {}
        self.toc = [tuple(t) for t in (toc or []) if len(t) >= 3]

    def _load(self, i: int) -> None:
        texts: list[str] = []
        ys: list[float | None] = []
        head: list[int] = []
        for x in (self._get(i) if 0 <= i < self.n else []):
            t, y = (x[0], x[1]) if isinstance(x, tuple) else (x, None)
            b = normalise_block(t)
            if not b:
                continue
            if len(texts) < HEAD_BLOCKS or (y is not None and y <= HEAD_TOP_FRAC):
                head.append(len(texts))
            texts.append(b)
            ys.append(None if y is None else float(y))
        self._cache[i], self._heads[i], self._ys[i] = texts, head, ys

    def blocks(self, i: int) -> list[str]:
        if i not in self._cache:
            self._load(i)
        return self._cache[i]

    def head_index(self, i: int) -> list[int]:
        """Indices into blocks(i) of the page's head blocks."""
        self.blocks(i)
        return self._heads[i]

    def head(self, i: int) -> list[str]:
        b = self.blocks(i)
        return [b[k] for k in self._heads[i]]

    def ys(self, i: int) -> list[float | None]:
        self.blocks(i)
        return self._ys[i]

    @property
    def read(self) -> int:
        return len(self._cache)

    @classmethod
    def from_lists(cls, pages: Sequence[Sequence[str]], toc: Sequence[Sequence[Any]] | None = None) -> "Pages":
        return cls(len(pages), lambda i: list(pages[i]), toc)


def _outline_pages(pages: Pages, rx: re.Pattern, *, fullmatch: bool) -> tuple[int, int | None] | None:
    """(start index, end index exclusive or None) of the first outline entry whose title matches."""
    toc = pages.toc
    for k, (level, title, page, *_) in enumerate(toc):
        t = _key(str(title))
        hit = rx.fullmatch(t) if fullmatch else rx.search(t)
        if not hit or not isinstance(page, int) or page < 1 or page > pages.n:
            continue
        start = page - 1
        end = None
        for lv2, _t2, p2, *_ in toc[k + 1:]:
            if isinstance(p2, int) and p2 > page and lv2 <= level:
                end = p2 - 1
                break
        return start, end
    return None


def is_divider_page(blocks: Sequence[str]) -> bool:
    """A section divider ('Statutory Reports: Management Discussion and Analysis 52 / Board's Report 78 / Report on
    Corporate Governance 110'): at least two other chapter names in short blocks and little prose. Too few names for
    is_toc_page, but it opens no chapter."""
    prose = sum(len(b) for b in blocks if len(b) > 90 and is_texty(b))
    names = sum(1 for b in blocks if len(b) <= 90 and _SECTION_NAME_RX.search(_key(b))
                and not _MDA_TITLE_RX.search(_key(b)))
    return names >= 2 and prose < MIN_SECTION_CHARS


def _mda_end(pages: Pages, start: int, divider: bool) -> int:
    """End (exclusive) of an MD&A starting at `start` without an outline end: the next end heading, at most
    MDA_MAX_PAGES pages. A block already printed on an MD&A page is a section tab, not an end heading; a divider's
    own chapter names are real chapters, so its blocks are not taken as tabs."""
    end = min(pages.n, start + MDA_MAX_PAGES)
    seen = set() if divider else _short_keys(pages.blocks(start))
    for i in range(start + 1, end):
        if page_end_heading(pages.head(i), seen) is not None:
            return i
        seen |= _short_keys(pages.blocks(i))
    return end


def find_mda(pages: Pages) -> tuple[int, int, str] | None:
    """(start, end exclusive, how) of the MD&A chapter, or None.

    Candidates in order: the outline entry (page verified within +-2), then every page with an MD&A heading that is
    not a contents page; section dividers (is_divider_page) only after all others. The first candidate whose range
    holds MIN_SECTION_CHARS of MD&A text wins (at most MDA_MAX_CANDIDATES are tried); if none does, the first one is
    returned (and fails later as too_short)."""
    def candidates() -> Iterable[tuple[int, int | None, str, bool]]:
        taken = None
        ol = _outline_pages(pages, _MDA_TITLE_RX, fullmatch=False)
        if ol is not None:
            s0, e0 = ol
            for s in sorted(range(max(0, s0 - 2), min(pages.n, s0 + 3)), key=lambda i: (abs(i - s0), i)):
                if page_has_mda_heading(pages.head(s)):
                    taken = s
                    yield s, (min(e0, s + MDA_MAX_PAGES) if e0 is not None and e0 > s else None), "outline", False
                    break
        dividers = []
        for i in range(pages.n):
            if i == taken or not page_has_mda_heading(pages.head(i)) or is_toc_page(pages.head(i), pages.blocks(i)):
                continue
            if is_divider_page(pages.blocks(i)):
                dividers.append(i)
                continue
            yield i, None, "heading", False
        for i in dividers:
            yield i, None, "heading", True

    first = None
    for n, (s, e, how, divider) in enumerate(candidates()):
        rng = (s, max(e if e is not None else _mda_end(pages, s, divider), s + 1), how)
        if first is None:
            first = rng
        body, _ = _mda_text(pages, rng[0], rng[1], running_blocks(pages, range(rng[0], rng[1])))
        if len(body.strip()) >= MIN_SECTION_CHARS or n + 1 >= MDA_MAX_CANDIDATES:
            return rng if len(body.strip()) >= MIN_SECTION_CHARS else first
    return first


def overview_text(pages: Pages, start: int, end: int) -> str:
    return clip("\n".join(range_blocks(pages, start, end,
                                       running_blocks(pages, range(start, min(pages.n, start + 6))))),
                OVERVIEW_MAX_CHARS)


def _overview_heading(block: str) -> bool:
    """A front-matter overview heading. 'Business Overview' is not one in the heading scan: it is a directors'
    report item (_DIRECTORS_RX) far more often than a front-matter page (the outline may still name it)."""
    k = _key(block)
    return len(block) <= 60 and bool(_OVERVIEW_RX.fullmatch(k)) and not k.startswith("business overview")


def find_overview(pages: Pages, before: int) -> tuple[int, int, str] | None:
    """(start, end exclusive, how) of a front-matter company overview page range before page `before`: the outline
    entry, else the first heading page. A candidate with under MIN_SECTION_CHARS // 2 of text (a numbers-only
    'at a glance' page) gives way to the next; pages of the directors' / Board's report are not front matter."""
    ol = _outline_pages(pages, _OVERVIEW_RX, fullmatch=True)
    if ol is not None and ol[0] < before:
        s, e = ol
        e = min(e if e is not None and e > s else s + 1, s + OVERVIEW_MAX_PAGES, before)
        e = max(e, s + 1)
        if len(overview_text(pages, s, e)) >= MIN_SECTION_CHARS // 2:
            return s, e, "outline"
    for i in range(min(before, pages.n)):
        b = pages.blocks(i)
        head = pages.head(i)
        if is_toc_page(head, b) or any(_BOARD_REPORT_RX.match(_key(x)) for x in head):
            continue
        if any(_overview_heading(x) for x in b[:30]) and len(overview_text(pages, i, i + 1)) >= MIN_SECTION_CHARS // 2:
            return i, i + 1, "heading"
    return None


def running_blocks(pages: Pages, idx: Iterable[int]) -> set[str]:
    """Short blocks repeated on RUNNING_MIN_PAGES+ of the given pages (running headers / footers / section tabs)."""
    seen: dict[str, int] = {}
    for i in idx:
        for k in {_run_key(b) for b in pages.blocks(i) if len(b) <= 100}:
            seen[k] = seen.get(k, 0) + 1
    return {k for k, n in seen.items() if n >= RUNNING_MIN_PAGES}


def range_blocks(pages: Pages, start: int, end: int, running: set[str] | None = None,
                 skip_first: set[int] | frozenset = frozenset()) -> list[str]:
    """Texty blocks of pages [start, end) without running headers; `skip_first`: block indices of page `start` to
    leave out (what precedes a chapter heading that starts mid-page)."""
    run = running if running is not None else running_blocks(pages, range(start, end))
    out = []
    for i in range(start, end):
        for k, b in enumerate(pages.blocks(i)):
            if (i == start and k in skip_first) or _run_key(b) in run or not is_texty(b):
                continue
            out.append(b)
    return out


def before_heading(pages: Pages, i: int) -> set[int]:
    """Blocks of page i that come before its MD&A heading in the content stream and lie above it on the page (the
    end of the previous chapter). Without positions: every block before it. A heading drawn last but placed at the
    top (designed reports) removes nothing."""
    pos = mda_heading_pos(pages.head(i))
    if pos is None:
        return set()
    k = pages.head_index(i)[pos]
    ys = pages.ys(i)
    yh = ys[k]
    return {j for j in range(k) if yh is None or ys[j] is None or ys[j] < yh}


def clip(text: str, limit: int) -> str:
    """At most `limit` chars, cut after the last sentence end in the final 40% when there is one."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    pos = max(cut.rfind(". "), cut.rfind(".\n"), cut.rfind("\n"))
    return cut[:pos + 1].rstrip() if pos >= limit * 0.6 else cut.rstrip()


def has_text_layer(pages: Pages) -> bool:
    return any(sum(len(b) for b in pages.blocks(i)) >= TEXT_LAYER_MIN_CHARS
               for i in range(min(pages.n, TEXT_LAYER_PROBE_PAGES)))


def _mda_text(pages: Pages, start: int, end: int, running: set[str]) -> tuple[str, str]:
    blocks = range_blocks(pages, start, end, running, skip_first=before_heading(pages, start))
    while blocks and len(blocks[0]) <= 140 and (_MDA_TITLE_RX.search(_key(blocks[0]))
                                                 or _key(blocks[0]).startswith("annexure")):
        blocks = blocks[1:]
    offset = 0
    for i, b in enumerate(blocks):
        if len(b) <= 80 and _BUSINESS_RX.match(_key(b)):
            if offset >= MDA_LEAD_CHARS:
                lead = clip("\n".join(blocks[:i]), MDA_LEAD_KEEP_CHARS)
                return "\n".join(blocks[i:]) + "\n\n" + lead, "business_first"
            return "\n".join(blocks), "opening"
        offset += len(b) + 1
    return "\n".join(blocks), "opening"


def _para_after(blocks: Sequence[str], j: int) -> str:
    """The prose blocks after heading j, up to the next short heading-like block."""
    out: list[str] = []
    for x in blocks[j + 1:j + 12]:
        if out and len(x) <= 60 and not x.rstrip().endswith((".", ",", ";")):
            break
        if is_texty(x):
            out.append(x)
    return clip("\n".join(out), FALLBACK_MAX_CHARS)


def _fallback(pages: Pages) -> tuple[str, str] | None:
    """(text, how) when there is no MD&A and no overview page: the financial statements' 'Company information' /
    'Corporate information' note (Ind AS note 1 says what the company does), else the directors' report 'State of
    the Company's affairs' / 'Nature of business' paragraph. At least FALLBACK_MIN_CHARS and business words."""
    found: dict[str, str] = {}
    for i in range(pages.n):
        blocks = pages.blocks(i)
        for j, b in enumerate(blocks):
            if len(b) > 80:
                continue
            k = _key(b)
            how = ("corporate_information" if _CORP_INFO_RX.match(k)
                   else "directors_report" if _DIRECTORS_RX.match(k) else None)
            if how is None or how in found:
                continue
            text = _para_after(blocks, j)
            if len(text) >= FALLBACK_MIN_CHARS and _BUSINESSY_RX.search(text):
                found[how] = text
                if how == "corporate_information":
                    return text, how
    if "directors_report" in found:
        return found["directors_report"], "directors_report"
    return None


def extract_section(pages: Pages) -> tuple[str | None, str]:
    """(section text or None, note). Note parts: 'overview:<how>', 'mda:<how>', 'mda_part:<opening|business_first>',
    'overview_only', 'fallback:corporate_information' / 'fallback:directors_report', 'pages:<read>/<total>', failures 'no_text_layer' / 'no_section' /
    'too_short:<chars>'."""
    def done(text: str | None, parts: list[str]) -> tuple[str | None, str]:
        parts.append(f"pages:{pages.read}/{pages.n}")
        return text, ";".join(parts)

    if pages.n == 0 or not has_text_layer(pages):
        return done(None, ["no_text_layer"])
    mda = find_mda(pages)
    before = mda[0] if mda else min(pages.n, 60)
    ov = find_overview(pages, before)
    parts: list[str] = []
    texts: list[str] = []
    if ov is not None:
        ov_text = overview_text(pages, ov[0], ov[1])
        if len(ov_text) >= MIN_SECTION_CHARS // 2:
            texts.append(ov_text)
            parts.append(f"overview:{ov[2]}")
    if mda is not None:
        s, e, how = mda
        running = running_blocks(pages, range(s, e))
        body, part = _mda_text(pages, s, e, running)
        if body.strip():
            texts.append(body)
            parts += [f"mda:{how}", f"mda_part:{part}"]
    if not any(p.startswith("mda:") for p in parts):
        if texts:
            parts.append("overview_only")
        else:
            fb = _fallback(pages)
            if fb is None:
                return done(None, ["no_section"])
            texts.append(fb[0])
            parts.append(f"fallback:{fb[1]}")
    text = clip("\n\n".join(t.strip() for t in texts if t.strip()), MAX_SECTION_CHARS)
    if len(text) < (FALLBACK_MIN_CHARS if parts[-1].startswith("fallback:") else MIN_SECTION_CHARS):
        return done(None, [f"too_short:{len(text)}"] + parts)
    return done(text, parts)


def _open_pymupdf(data: bytes) -> tuple[Any, Pages]:
    import fitz
    try:
        fitz.TOOLS.mupdf_display_errors(False)
    except Exception:
        pass
    doc = fitz.open(stream=data, filetype="pdf")

    def get(i: int) -> list[tuple[str, float]]:
        page = doc[i]
        h = float(page.rect.height) or 1.0
        return [(b[4], max(0.0, float(b[1]) - float(page.rect.y0)) / h)
                for b in page.get_text("blocks", sort=False) if len(b) > 6 and b[6] == 0]
    try:
        toc = doc.get_toc(simple=True)
    except Exception:
        toc = []
    return doc, Pages(doc.page_count, get, toc)


def extract_from_pdf(data: bytes) -> tuple[str | None, str, str]:
    """(section, note, backend). PyMuPDF (text blocks, outline, lazy pages); pypdf lines as the fallback. Serialised
    by cninfo's process-wide PDF lock (PyMuPDF is not thread-safe)."""
    with cninfo._PDF_LOCK:
        try:
            doc, pages = _open_pymupdf(data)
        except ImportError:
            doc = None
        except Exception:  # malformed for MuPDF: let pypdf try
            doc = None
        else:
            try:
                sect, note = extract_section(pages)
                return sect, note, "pymupdf"
            except Exception as e:  # noqa: BLE001 - a page MuPDF cannot read: a failed extraction, not a crash
                return None, f"pdf_parse_failed:{type(e).__name__}", "pymupdf"
            finally:
                doc.close()
        try:
            texts, backend = cninfo._pdf_pages_locked(data, "pypdf")
        except (cninfo.PdfError, Exception) as e:  # noqa: BLE001
            return None, f"pdf_parse_failed:{type(e).__name__}", "none"
        pages = Pages.from_lists([[ln for ln in t.splitlines()] for t in texts])
        sect, note = extract_section(pages)
        return sect, note, backend


def short_description(section: str | None, max_chars: int = SHORT_DESC_CHARS) -> str:
    """The first ~max_chars of the section, cut at a sentence end when possible."""
    if not section:
        return ""
    text = "\n".join(p.strip() for p in section.split("\n") if p.strip())
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    pos = max(cut.rfind(". "), cut.rfind(".\n"))
    return cut[:pos + 1] if pos >= max_chars * 0.4 else cut.rstrip() + "…"


# =========================================================================== fetching


def non_pdf_body(resp: Any) -> str | None:
    return cninfo.non_pdf_body(resp)


_TITLE_RE = re.compile(rb"<title[^>]*>(.{0,300}?)</title>", re.I | re.S)
_DENIAL_TITLE_RE = re.compile(r"denied|forbidden|captcha|challenge|block|reject|error|verif|attention|just\s+a\s+moment"
                              r"|too\s+many|unavailable|security|robot|bot\b", re.I)
_DENIAL_BODY_RE = re.compile(r"access\s+denied|reference\s*#\s*\d|captcha|request\s+(?:rejected|unsuccessful|blocked)"
                             r"|unusual\s+traffic|too\s+many\s+requests|you\s+have\s+been\s+blocked|incapsula"
                             r"|challenge-platform|cf-chl", re.I)


def is_site_home_page(body: bytes | None) -> bool:
    """True only for a page positively identified as the BSE site's own page (what its SPA answers for an unknown
    path): a <title> naming BSE and no denial / challenge wording anywhere in the first 64 KB. Anything else served
    as HTML in place of a PDF is treated as a soft block."""
    head = (body or b"")[:65536]
    m = _TITLE_RE.search(head)
    title = m.group(1).decode("utf-8", "replace") if m else ""
    if not re.search(r"\bbse\b", title, re.I) or _DENIAL_TITLE_RE.search(title):
        return False
    return _DENIAL_BODY_RE.search(head.decode("utf-8", "replace")) is None


def _looks_like_page(resp: Any) -> bool:
    headers = getattr(resp, "headers", None) or {}
    ctype = next((str(v) for k, v in headers.items() if str(k).lower() == "content-type"), "").lower()
    return "html" in ctype or (resp.body or b"")[:1024].lstrip()[:1] == b"<"


def _block(client: Any, url: str, status: int | None, reason: str) -> Blocked:
    """A Blocked raised by this adapter (redirect / page instead of JSON or PDF): the client's halt is set at once,
    as http.Client does for a 403 / 429, so queued PDF workers send nothing more."""
    ev = getattr(client, "halt", None)
    if isinstance(ev, threading.Event):
        ev.set()
    return Blocked(url, status, reason)


@dataclasses.dataclass
class CompanyResult:
    """What fetch_company produced for one scrip code (no database). status: ok | no_annual_report | extract_failed |
    error | skipped_unchanged | skipped_failed. exc: Blocked / Halted / a network error (status None)."""
    code: str
    status: str | None = None
    http_status: int | None = None
    note: str | None = None
    doc: dict | None = None               # the selected listing row
    section: str | None = None
    extract_note: str | None = None
    backend: str | None = None
    raw_sha256: str | None = None
    raw_bytes: int | None = None
    listing_raw: bytes | None = None
    exc: BaseException | None = None
    requests: int = 0
    list_s: float = 0.0
    pdf_s: float = 0.0
    extract_s: float = 0.0


def list_reports(client: Any, code: str, res: CompanyResult) -> list[dict] | None:
    """GET the annual report listing of one scrip; None when it failed (res.status / note set). A redirect (BSE sends
    refused API calls to a members page) or an HTML page instead of JSON (a WAF / denial page served as 200) raises
    Blocked ('redirect_refusal' / 'non_json_page'): the run stops at the first one."""
    url = AR_LIST_URL.format(code=urllib.parse.quote(str(code)))
    t = time.monotonic()
    try:
        resp = client.get(url, headers=API_HEADERS, deadline_s=LIST_DEADLINE_S)
    finally:
        res.list_s += time.monotonic() - t
        res.requests += 1
    if 300 <= resp.status < 400:
        raise _block(client, url, resp.status, "redirect_refusal")
    if resp.status != 200:
        res.status, res.http_status, res.note = "error", resp.status, f"list_http_{resp.status}"
        return None
    try:
        rows = parse_ar_listing(resp.body)
    except (ValueError, UnicodeDecodeError) as e:
        if _looks_like_page(resp):
            raise _block(client, url, resp.status, "non_json_page") from None
        res.status, res.http_status, res.note = "error", 200, f"list_bad_json:{type(e).__name__}"
        return None
    res.listing_raw = resp.body
    return rows


def fetch_pdf(client: Any, doc: Mapping[str, Any], res: CompanyResult) -> None:
    """Download + extract the selected report into res (status ok / extract_failed / error). Raises Blocked /
    Halted; a network error / deadline is left in res.exc. An HTML page instead of the PDF is a soft block
    (Blocked 'non_pdf_body', like cninfo) unless is_site_home_page() identifies the site's own page (a dead link)."""
    t = time.monotonic()
    try:
        resp = client.get(doc["url"], headers=PDF_HEADERS, max_bytes=MAX_PDF_BYTES, deadline_s=PDF_DEADLINE_S)
    except (OSError, http.client.HTTPException) as e:
        res.exc = e
        return
    finally:
        res.pdf_s += time.monotonic() - t
        res.requests += 1
    if resp.status != 200:
        res.status, res.http_status, res.note = "error", resp.status, f"pdf_http_{resp.status}"
        return
    bad = non_pdf_body(resp)
    if bad == "page" and not is_site_home_page(resp.body):
        raise _block(client, getattr(resp, "url", None) or doc["url"], resp.status, "non_pdf_body")
    if bad is not None:
        res.status, res.http_status = "error", resp.status
        res.note = "pdf_html_page" if bad == "page" else "pdf_not_pdf"
        return
    raw = resp.body or b""
    res.raw_sha256, res.raw_bytes = store.sha256(raw), len(raw)
    if getattr(resp, "truncated", False):
        res.status, res.http_status = "extract_failed", 200
        res.extract_note = res.note = f"too_large:>{MAX_PDF_BYTES // (1024 * 1024)}MB"
        res.backend = "none"
        return
    t = time.monotonic()
    sect, note, backend = extract_from_pdf(raw)
    res.extract_s += time.monotonic() - t
    res.section, res.extract_note, res.backend = sect, note, backend
    res.status, res.http_status = ("ok" if sect else "extract_failed"), 200
    res.note = note


def _as_date(v: Any) -> dt.date | None:
    if isinstance(v, dt.datetime):
        return v.date()
    return v if isinstance(v, dt.date) else None


def is_stale(doc: Mapping[str, Any], today: dt.date) -> bool:
    """The label is behind the one that must be out under a 31 March fiscal year (expected_latest_label) AND the
    filing is older than STALE_MIN_AGE_DAYS (when its date is known). The age test keeps a June / September /
    December year-end company, whose next report is not yet due, from being called stale in October."""
    if doc["year"] >= expected_latest_label(today):
        return False
    filed = _as_date(doc.get("filing_date"))
    return filed is None or (today - filed).days > STALE_MIN_AGE_DAYS


def doc_note(doc: Mapping[str, Any], today: dt.date) -> str:
    parts = [f"fy_label:{doc['year']}"]
    if doc.get("revised"):
        parts.append("revision")
    if is_stale(doc, today):
        parts.append(f"stale:{doc['year']}")
    return ";".join(parts)


def fetch_company(client: Any, code: str, *, stored: Mapping[tuple[str, str], str] | None = None,
                  refresh: bool = True, today: dt.date | None = None) -> CompanyResult:
    """Single-company path without any database: listing -> latest report -> PDF -> business section.
    `stored` ((code, url) -> 'ok' | 'failed') lets sync skip a report it already processed (unless refresh).
    Blocked / Halted propagate."""
    res = CompanyResult(code=str(code))
    try:
        rows = list_reports(client, code, res)
    except (OSError, http.client.HTTPException) as e:
        res.exc = e
        return res
    if rows is None:
        return res
    doc = select_latest(rows)
    if doc is None:
        res.status, res.http_status, res.note = "no_annual_report", 200, "no_annual_report"
        return res
    res.doc = doc
    prior = (stored or {}).get((str(code), doc["url"]))
    if prior is not None and not refresh:
        res.status = "skipped_unchanged" if prior == "ok" else "skipped_failed"
        return res
    fetch_pdf(client, doc, res)
    if res.status in ("ok", "extract_failed"):
        extra = doc_note(doc, today or _today())
        res.extract_note = f"{res.extract_note};{extra}" if res.extract_note else extra
    return res


# =========================================================================== sync (database)

_DOC_COLS = ["doc_id", "security_id", "company_key", "source_id", "cik", "form", "section", "accession",
             "filing_date", "report_date", "url", "raw_sha256", "raw_bytes", "text_path", "text_sha256", "text_chars",
             "extractor", "extract_note", "fetched_at", "snapshot_id"]
_DESC_COLS = ["security_id", "source_id", "company_key", "text", "text_sha256", "lang", "source_url", "fetched_at",
              "match_method", "match_score", "snapshot_id"]
_STATE_COLS = ["source_id", "security_id", "status", "http_status", "attempts", "last_attempt_at", "note"]


def text_path_for(cfg: Config, code: str, accession: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.\-]+", "_", str(accession)) or "_"
    return Path(cfg.home) / "docs" / "bse" / str(code) / f"{safe}-{SECTION}.txt"


def doc_id_for(code: str, accession: str) -> str:
    return f"{SOURCE_ID}:{code}:{accession}:{SECTION}"


class _Batch:
    def __init__(self) -> None:
        self.listings: list[dict] = []
        self.docs: list[dict] = []
        self.descs: list[dict] = []
        self.states: list[list[Any]] = []
        self.started_at: dt.datetime | None = None
        self.duration_s = 0.0
        self.companies = 0

    def __len__(self) -> int:
        return len(self.states) + len(self.listings) + len(self.docs)


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v


def _flush(cfg: Config, batch: _Batch, attempts: dict[str, int], *, wait_s: float = FINAL_WAIT_S) -> int:
    """One short write session: snapshots (listing batch, report batch) + documents + descriptions + crawl_state."""
    if not len(batch):
        return 0
    snaps = 0
    l_manifest = json.dumps(batch.listings, sort_keys=True).encode() if batch.listings else None
    d_manifest = (json.dumps([{k: _iso(v) for k, v in d.items()} for d in batch.docs], sort_keys=True).encode()
                  if batch.docs else None)
    with store.session(cfg, wait_s=wait_s) as con:
        l_path = l_dig = d_path = d_dig = None
        if l_manifest is not None:
            l_path, l_dig = store.save_raw(cfg, SOURCE_ID, "list_batch", l_manifest)
        if d_manifest is not None:
            d_path, d_dig = store.save_raw(cfg, SOURCE_ID, "report_batch", d_manifest)
        con.begin()
        try:
            if batch.listings:
                store.record_snapshot(
                    con, source_id=SOURCE_ID, kind="list_batch",
                    request={"method": "GET", "url": AR_LIST_URL, "codes": [x["code"] for x in batch.listings]},
                    raw_path=l_path, raw_sha256=l_dig, raw_bytes=len(l_manifest), rows=len(batch.listings),
                    duration_s=round(batch.duration_s, 3), fetched_at=batch.started_at)
                snaps += 1
            snap_id = None
            if batch.docs:
                snap_id = store.record_snapshot(
                    con, source_id=SOURCE_ID, kind="report_batch",
                    request={"method": "GET", "urls": [d["url"] for d in batch.docs]},
                    raw_path=d_path, raw_sha256=d_dig, raw_bytes=len(d_manifest), rows=len(batch.docs),
                    duration_s=round(batch.duration_s, 3), fetched_at=batch.started_at)
                snaps += 1
            store.upsert_many(con, "documents", _DOC_COLS,
                              [[d.get(c) if c != "snapshot_id" else snap_id for c in _DOC_COLS] for d in batch.docs])
            store.upsert_many(con, "descriptions", _DESC_COLS,
                              [[d.get(c) if c != "snapshot_id" else snap_id for c in _DESC_COLS]
                               for d in batch.descs])
            store.upsert_many(con, "crawl_state", _STATE_COLS, batch.states)
            con.commit()
        except BaseException:
            con.rollback()
            raise
    for r in batch.states:
        attempts[r[1]] = r[4]
    return snaps


def _load_lines(con, only_universe: bool) -> list[dict]:
    cols = ["security_id", "exchange", "symbol", "company_key", "market_cap_usd", "isin"]
    where = "upper(exchange) IN ('BSE', 'NSE')"
    if only_universe:
        sql = f"SELECT {', '.join(cols)} FROM universe WHERE {where}"
    else:
        sql = (f"SELECT s.security_id, s.exchange, s.symbol, s.company_key, lm.market_cap_usd, s.isin "
               f"FROM securities s LEFT JOIN latest_market lm USING (security_id) WHERE s.active AND "
               f"upper(s.exchange) IN ('BSE', 'NSE')")
    return [r for r in (dict(zip(cols, x)) for x in con.execute(sql).fetchall()) if is_in_line(r)]


def _norm_codes(codes: Iterable[str] | None) -> set[str] | None:
    if codes is None:
        return None
    if isinstance(codes, str):
        codes = [c for c in re.split(r"[,\s]+", codes) if c]
    return {str(c).strip().upper().rsplit(":", 1)[-1] for c in codes if str(c).strip()}


def reusable_scrip_list(cfg: Config, today: dt.date | None = None, max_age_days: int = SCRIP_LIST_REUSE_DAYS,
                        min_rows: int = MIN_SCRIP_ROWS) -> tuple[bytes, str] | None:
    """(raw bytes, path) of the newest scrip list an earlier run saved (day folder <= max_age_days old), else None."""
    folder = Path(cfg.raw_dir) / SOURCE_ID
    if not folder.is_dir():
        return None
    today = today or store.now_utc().date()
    for day in sorted((p for p in folder.iterdir() if p.is_dir()), reverse=True):
        try:
            d = dt.date.fromisoformat(day.name)
        except ValueError:
            continue
        if (today - d).days > max_age_days or d > today:
            continue
        for f in sorted(day.glob("scrip_list-*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                raw = f.read_bytes()
                if len(parse_scrip_list(raw)) >= min_rows:
                    return raw, str(f)
            except (OSError, ValueError):
                continue
    return None


def clamp_workers(workers: Any) -> int:
    try:
        n = int(workers)
    except (TypeError, ValueError):
        n = DEFAULT_WORKERS
    return max(1, min(n, MAX_WORKERS))


def sync(cfg: Config, client: Any = None, *, limit: int | None = None, codes: Iterable[str] | None = None,
         refresh: bool = False, min_mcap_usd: float | None = None, batch_size: int = 20, only_universe: bool = True,
         progress_every: int = 25, on_blocked: Callable[[BaseException], Any] | None = None,
         workers: int | None = DEFAULT_WORKERS, reuse_scrip_list: bool = True,
         min_scrip_rows: int = MIN_SCRIP_ROWS,
         on_company: Callable[[list[str], str, str | None], None] | None = None) -> dict:
    """Map Indian universe lines to BSE scrip codes and pull the latest annual report's business section.

    Steps: (1) the scrip list (reused when an earlier run saved one <= SCRIP_LIST_REUSE_DAYS ago, else GET), snapshot,
    identifiers 'bse_scrip_code' upserted and stale ones of considered lines dropped; (2) distinct scrip codes by
    market cap desc, filtered by codes (scrip code, BSE scrip id / TradingView symbol or security_id), min_mcap_usd,
    limit; companies already holding the newest possible year are skipped (unless refresh); (3) per company the
    listing on the main thread, the PDF + extraction on `workers` threads; per batch one short write session.
    Returns a summary dict whose 'status' is ok | blocked | stopped_errors | interrupted | store_locked | error.
    `client`: http.Client-compatible (get(url, headers=, max_bytes=, deadline_s=)); None builds one from cfg. Its
    min_interval_s is raised to DEFAULT_MIN_INTERVAL_S if lower.
    on_company(security_ids, status, note): called on the main thread for every company whose status is recorded,
    and with 'skipped_current' / 'skipped_unchanged' / 'skipped_known_failed' for a company settled without a new
    download (the on-demand fetch's per-company events); its errors are ignored."""
    if client is None:
        client = Client(user_agent=cfg.user_agent, min_interval_s=max(DEFAULT_MIN_INTERVAL_S, cfg.min_interval_s),
                        timeout_s=cfg.timeout_s)
    cur = getattr(client, "min_interval_s", None)
    if isinstance(cur, (int, float)) and cur < DEFAULT_MIN_INTERVAL_S:
        client.min_interval_s = DEFAULT_MIN_INTERVAL_S
    budget = guard.RATE_BUDGETS.get(COMMAND, "bse")
    with guard.budget_lock(cfg, budget, reentrant=True):
        return _sync(cfg, client, limit=limit, codes=_norm_codes(codes), refresh=refresh, min_mcap_usd=min_mcap_usd,
                     batch_size=max(1, int(batch_size)), only_universe=only_universe, progress_every=progress_every,
                     on_blocked=on_blocked, workers=clamp_workers(workers), reuse_scrip_list=reuse_scrip_list,
                     min_scrip_rows=int(min_scrip_rows), on_company=on_company)


class _Pool:
    """Minimal executor on daemon threads (a stalled PDF abandoned after DRAIN_MAX_S never keeps the process alive)."""

    def __init__(self, workers: int) -> None:
        self._q: queue_mod.SimpleQueue = queue_mod.SimpleQueue()
        self._threads = [threading.Thread(target=self._run, name=f"bse-pdf-{i}", daemon=True) for i in range(workers)]
        for t in self._threads:
            t.start()

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            fut, fn, args = item
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn(*args))
            except BaseException as e:  # noqa: BLE001 - reported through the Future
                fut.set_exception(e)

    def submit(self, fn: Callable, *args: Any) -> Future:
        fut: Future = Future()
        self._q.put((fut, fn, args))
        return fut

    def shutdown(self) -> None:
        for _ in self._threads:
            self._q.put(None)


def _sync(cfg: Config, client: Any, *, limit, codes, refresh, min_mcap_usd, batch_size, only_universe,
          progress_every, on_blocked, workers, reuse_scrip_list, min_scrip_rows, on_company=None) -> dict:
    run_id = f"{COMMAND}-{uuid.uuid4().hex[:12]}"
    requests_before = int(getattr(client, "requests_made", 0) or 0)
    if workers > 1 and getattr(client, "thread_safe", False) is not True:
        workers = 1
    today = _today()
    counts = {s: 0 for s in CRAWL_STATUSES}
    summary: dict[str, Any] = {
        "run_id": run_id, "workers": workers, "scrip_list_rows": 0, "scrip_list_reused": None, "in_lines": 0,
        "securities_mapped": 0, "unmatched": 0, "unmatched_by_exchange": {}, "ambiguous": [],
        "identifiers_removed": 0, "queued": 0, "attempted": 0, "skipped_current": 0, "skipped_unchanged": 0,
        "ok_by_part": {}, "failures_by_note": {}, "requests": 0, "list_requests": 0, "pdfs": 0,
        "bytes_downloaded": 0, "pdf_bytes": 0, "mean_pdf_mb": None, "list_s": 0.0, "pdf_s": 0.0, "extract_s": 0.0,
        "wall_s": None, "seconds_per_company": None, "snapshots": 0, "stopped_reason": None, "blocked_url": None,
        "blocked_status": None, "blocked_reason": None, "abandoned_in_flight": 0, "status": None}

    def log(msg: str) -> None:
        print(f"[bse] {msg}", file=sys.stderr, flush=True)

    mark_lock = threading.Lock()

    def mark(url: str | None, status: Any, reason: Any, exc: BaseException | None = None) -> None:
        """The run's first block only (main thread or a PDF worker): cooldown marker first (survives a failing DB
        write), then the caller's callback."""
        with mark_lock:
            if summary["blocked_reason"] is not None:
                return
            guard.mark_blocked(cfg, COMMAND, url=url, status=status, reason=reason)
            summary.update(blocked_url=url, blocked_status=status, blocked_reason=reason)
        if on_blocked is not None and exc is not None:
            try:
                on_blocked(exc)
            except Exception:
                pass

    with store.session(cfg, wait_s=FINAL_WAIT_S) as con:
        con.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
                    [run_id, COMMAND, store.now_utc(), None, "running", 0, None])

    def finish(status: str, note: Any = None) -> dict:
        summary["requests"] = int(getattr(client, "requests_made", 0) or 0) - requests_before
        summary["status"] = status
        summary["counts"] = dict(counts)
        if summary["pdfs"]:
            summary["mean_pdf_mb"] = round(summary["pdf_bytes"] / summary["pdfs"] / 1e6, 2)
        for k in ("list_s", "pdf_s", "extract_s"):
            summary[k] = round(summary[k], 2)
        payload = json.dumps({"counts": counts, "stopped_reason": summary["stopped_reason"], "workers": workers,
                              "queued": summary["queued"], "skipped_current": summary["skipped_current"],
                              **({"blocked_url": summary["blocked_url"]} if summary["blocked_url"] else {}),
                              **({"note": str(note)} if note else {})}, default=str)
        with store.session(cfg, wait_s=FINAL_WAIT_S) as con:
            con.execute("UPDATE runs SET finished_at = ?, status = ?, requests = ?, note = ? WHERE run_id = ?",
                        [store.now_utc(), status, summary["requests"], payload, run_id])
        return summary

    # ---- (1) scrip list -----------------------------------------------------------------------------------
    t0 = time.monotonic()
    fetched_at = store.now_utc()
    raw = None
    reused = reusable_scrip_list(cfg, min_rows=min_scrip_rows) if reuse_scrip_list else None
    if reused is not None:
        raw, summary["scrip_list_reused"] = reused
    else:
        try:
            resp = client.get(SCRIP_LIST_URL, headers=API_HEADERS, deadline_s=SCRIP_LIST_DEADLINE_S)
            summary["bytes_downloaded"] += len(resp.body or b"")
        except Blocked as e:
            mark(getattr(e, "url", None) or SCRIP_LIST_URL, getattr(e, "status", None), getattr(e, "reason", None), e)
            summary["stopped_reason"] = "blocked"
            return finish("blocked", f"scrip_list: {e.reason}")
        except KeyboardInterrupt:
            summary["stopped_reason"] = "interrupted"
            return finish("interrupted")
        except (OSError, http.client.HTTPException, Halted) as e:
            summary["stopped_reason"] = "scrip_list_error"
            return finish("error", f"scrip_list: {type(e).__name__}: {e}"[:300])
        if resp.status != 200:
            summary["stopped_reason"] = "scrip_list_error"
            return finish("error", f"scrip_list: http_{resp.status}")
        raw = resp.body
    try:
        scrip_rows = parse_scrip_list(raw)
    except (ValueError, UnicodeDecodeError) as e:
        summary["stopped_reason"] = "scrip_list_error"
        return finish("error", f"scrip_list: bad_json: {type(e).__name__}")
    summary["scrip_list_rows"] = len(scrip_rows)
    if len(scrip_rows) < min_scrip_rows:
        summary["stopped_reason"] = "scrip_list_suspect"
        return finish("error", f"scrip_list_suspect:rows={len(scrip_rows)}")
    report: dict = {}
    try:
        if reused is not None:     # never re-save a reused copy: its day folder decides when it expires
            raw_path, digest = summary["scrip_list_reused"], store.sha256(raw)
        else:
            raw_path, digest = store.save_raw(cfg, SOURCE_ID, "scrip_list", raw)
        with store.session(cfg, wait_s=FINAL_WAIT_S) as con:
            list_snap = store.record_snapshot(
                con, source_id=SOURCE_ID, kind="scrip_list",
                request={"method": "GET", "url": SCRIP_LIST_URL}, raw_path=raw_path, raw_sha256=digest,
                raw_bytes=len(raw), rows=len(scrip_rows), duration_s=round(time.monotonic() - t0, 3),
                note=f"reused_raw:{summary['scrip_list_reused']}" if summary["scrip_list_reused"] else None,
                fetched_at=fetched_at)
            summary["snapshots"] += 1
            lines = _load_lines(con, only_universe)
            mapping = map_securities_to_scrip(lines, scrip_rows, report=report)
            store.upsert_many(con, "identifiers", ["security_id", "id_type", "id_value", "method", "snapshot_id"],
                              [[sid, ID_TYPE, code, method, list_snap] for sid, code, method in mapping])
            unmapped = sorted({ln["security_id"] for ln in lines} - {sid for sid, _, _ in mapping})
            if unmapped:
                summary["identifiers_removed"] = con.execute(
                    "SELECT count(*) FROM identifiers WHERE id_type = ? AND list_contains(?::VARCHAR[], security_id)",
                    [ID_TYPE, unmapped]).fetchone()[0]
                con.execute("DELETE FROM identifiers WHERE id_type = ? AND list_contains(?::VARCHAR[], security_id)",
                            [ID_TYPE, unmapped])
            # (code, url) -> 'ok' (text extracted) | 'failed' (failed under the current extractor: not retried)
            stored: dict[tuple[str, str], str] = {}
            current: dict[str, int] = {}     # code -> newest year label extracted by the current extractor
            recent: dict[str, dt.date] = {}  # code -> newest filing date extracted by the current extractor
            for code, url, tp, ext, note, filed in con.execute(
                    "SELECT cik, url, text_path, extractor, extract_note, filing_date FROM documents "
                    "WHERE source_id = ?", [SOURCE_ID]).fetchall():
                key = (str(code), str(url))
                this_version = str(ext or "").startswith(EXTRACTOR_VERSION + "/")
                if tp is not None:
                    stored[key] = "ok"
                    m = _NOTE_FY_RE.search(str(note or ""))
                    if this_version and m:
                        current[str(code)] = max(current.get(str(code), 0), int(m.group(1)))
                    fd = _as_date(filed)
                    if this_version and fd is not None:
                        recent[str(code)] = max(recent.get(str(code), fd), fd)
                elif this_version:
                    stored.setdefault(key, "failed")
            attempts = {sid: n for sid, n in con.execute(
                "SELECT security_id, attempts FROM crawl_state WHERE source_id = ?", [SOURCE_ID]).fetchall()}
    except KeyboardInterrupt:
        summary["stopped_reason"] = "interrupted"
        return finish("interrupted")
    summary["in_lines"] = len(lines)
    summary["securities_mapped"] = len(mapping)
    summary["unmatched"] = report.get("unmatched", 0)
    summary["unmatched_by_exchange"] = report.get("unmatched_by_exchange", {})
    summary["ambiguous"] = report.get("ambiguous", [])

    # ---- (2) queue ----------------------------------------------------------------------------------------
    by_sid = {ln["security_id"]: ln for ln in lines}
    sid_scrip = {r["code"]: r["scrip_id"] for r in scrip_rows}
    groups: dict[str, list[dict]] = {}
    for sid, code, _ in mapping:
        groups.setdefault(code, []).append(by_sid[sid])
    for g in groups.values():
        g.sort(key=lambda ln: (-(ln["market_cap_usd"] or float("-inf")), ln["security_id"]))
    cap = {c: max((ln["market_cap_usd"] for ln in g if ln["market_cap_usd"] is not None), default=None)
           for c, g in groups.items()}
    queue = sorted(groups, key=lambda c: (-(cap[c] if cap[c] is not None else float("-inf")), c))
    if codes is not None:
        queue = [c for c in queue if c in codes or sid_scrip.get(c, "") in codes
                 or any(str(ln["security_id"]).upper().rsplit(":", 1)[-1] in codes for ln in groups[c])]
    if min_mcap_usd is not None:
        queue = [c for c in queue if cap[c] is not None and cap[c] >= float(min_mcap_usd)]
    if limit is not None:
        queue = queue[:max(0, int(limit))]
    newest = newest_possible_label(today)

    def is_current(c: str) -> bool:
        return current.get(c, 0) >= newest or (c in recent and (today - recent[c]).days < CURRENT_MAX_AGE_DAYS)

    def event(code: str, status: str, note: str | None) -> None:
        if on_company is not None:
            try:
                on_company([ln["security_id"] for ln in groups[code]], status, note)
            except Exception:  # noqa: BLE001 - a callback never stops the sync
                pass
    if not refresh:
        summary["skipped_current"] = sum(1 for c in queue if is_current(c))
        for c in queue:
            if is_current(c):
                event(c, "skipped_current", None)
        queue = [c for c in queue if not is_current(c)]
    summary["queued"] = len(queue)

    # ---- (3) fetch ----------------------------------------------------------------------------------------
    batch = _Batch()
    at_of: dict[str, dt.datetime] = {}
    consecutive_errors = 0
    halt = getattr(client, "halt", None)
    if not isinstance(halt, threading.Event):
        halt = threading.Event()
    pool = _Pool(workers)
    in_flight: dict[Future, str] = {}
    t_start = time.monotonic()
    done = 0

    def state(code: str, status: str, http_status: int | None, note: str | None, at: dt.datetime) -> None:
        for ln in groups[code]:
            sid = ln["security_id"]
            batch.states.append([SOURCE_ID, sid, status, http_status, (attempts.get(sid) or 0) + 1, at, note])
        counts[status] += 1
        if status != "ok":
            key = (note or status).split(";", 1)[0].split(":", 1)[0].strip() or status
            summary["failures_by_note"][key] = summary["failures_by_note"].get(key, 0) + 1
        event(code, status, note)

    def flush(wait_s: float = FINAL_WAIT_S) -> None:
        nonlocal batch
        summary["snapshots"] += _flush(cfg, batch, attempts, wait_s=wait_s)
        batch = _Batch()

    def meter(res: CompanyResult) -> None:
        summary["list_s"] += res.list_s
        summary["pdf_s"] += res.pdf_s
        summary["extract_s"] += res.extract_s
        batch.duration_s += res.list_s + res.pdf_s + res.extract_s
        if res.raw_bytes:
            summary["pdfs"] += 1
            summary["pdf_bytes"] += res.raw_bytes
            summary["bytes_downloaded"] += res.raw_bytes

    def apply(code: str, res: CompanyResult) -> str:
        """Record one company's finished result (main thread). Returns its crawl status (or 'skipped')."""
        nonlocal done
        at = at_of.get(code) or store.now_utc()
        meter(res)
        done += 1
        primary = groups[code][0]
        doc = res.doc
        if res.status in ("skipped_unchanged", "skipped_failed"):
            summary["skipped_unchanged"] += 1
            event(code, "skipped_unchanged" if res.status == "skipped_unchanged" else "skipped_known_failed", None)
            return "skipped"
        if res.status in ("ok", "extract_failed") and doc is not None:
            row = {"doc_id": doc_id_for(code, doc["accession"]), "security_id": primary["security_id"],
                   "company_key": primary["company_key"], "source_id": SOURCE_ID, "cik": code, "form": FORM,
                   "section": SECTION, "accession": doc["accession"], "filing_date": doc["filing_date"],
                   "report_date": None, "url": doc["url"], "raw_sha256": res.raw_sha256, "raw_bytes": res.raw_bytes,
                   "text_path": None, "text_sha256": None, "text_chars": None,
                   "extractor": f"{EXTRACTOR_VERSION}/{res.backend or 'none'}", "extract_note": res.extract_note,
                   "fetched_at": at}
            if res.section:
                tp = text_path_for(cfg, code, doc["accession"])
                tp.parent.mkdir(parents=True, exist_ok=True)
                payload = res.section.encode("utf-8")
                tp.write_bytes(payload)
                row.update(text_path=str(tp), text_sha256=store.sha256(payload), text_chars=len(res.section))
                desc = short_description(res.section)
                if desc:
                    for ln in groups[code]:
                        batch.descs.append({"security_id": ln["security_id"], "source_id": SOURCE_ID,
                                            "company_key": ln["company_key"], "text": desc,
                                            "text_sha256": store.sha256(desc.encode("utf-8")), "lang": "en",
                                            "source_url": doc["url"], "fetched_at": at, "match_method": ID_TYPE,
                                            "match_score": 1.0})
                part = next((p.split(":", 1)[1] for p in (res.extract_note or "").split(";")
                             if p.startswith("mda_part:")), None) or \
                    ("overview_only" if "overview_only" in (res.extract_note or "") else "fallback")
                summary["ok_by_part"][part] = summary["ok_by_part"].get(part, 0) + 1
            batch.docs.append(row)
        status = res.status or "error"
        note = res.extract_note if status in ("ok", "extract_failed") else res.note
        state(code, status, res.http_status, note, at)
        batch.companies += 1
        return status

    def handle(code: str, fut_res: CompanyResult | None, exc: BaseException | None) -> str | None:
        """Apply a finished PDF task; returns a stop reason or None."""
        nonlocal consecutive_errors
        if exc is None and fut_res is not None and fut_res.exc is not None:
            exc = fut_res.exc
            if not isinstance(exc, (Blocked, Halted)):
                meter(fut_res)
                at = at_of.get(code) or store.now_utc()
                state(code, "error", None, f"network:{type(exc).__name__}: {exc}"[:200], at)
                batch.companies += 1
                consecutive_errors += 1
                return "consecutive_errors" if consecutive_errors >= MAX_CONSECUTIVE_ERRORS else None
        if isinstance(exc, Blocked):
            mark(getattr(exc, "url", None), getattr(exc, "status", None), getattr(exc, "reason", None), exc)
            return "blocked"
        if isinstance(exc, Halted):
            return None
        if isinstance(exc, KeyboardInterrupt):
            raise exc
        if exc is not None:      # an unexpected failure inside one company's task: record it, go on
            state(code, "error", None, f"internal:{type(exc).__name__}: {exc}"[:200], at_of.get(code) or store.now_utc())
            batch.companies += 1
            consecutive_errors += 1
            return "consecutive_errors" if consecutive_errors >= MAX_CONSECUTIVE_ERRORS else None
        st = apply(code, fut_res)
        if st == "error":
            consecutive_errors += 1
        elif st != "skipped":
            consecutive_errors = 0
        return "consecutive_errors" if consecutive_errors >= MAX_CONSECUTIVE_ERRORS else None

    def collect(block: bool, timeout: float | None = None) -> str | None:
        if not in_flight:
            return None
        ready, _ = futures_wait(list(in_flight), timeout=(timeout if block else 0), return_when=FIRST_COMPLETED)
        reason = None
        for fut in ready:
            code = in_flight.pop(fut)
            exc = fut.exception()
            r = handle(code, None if exc else fut.result(), exc)
            reason = reason or r
        return reason

    def progress() -> None:
        if progress_every and done and done % progress_every == 0:
            el = time.monotonic() - t_start
            log(f"{done}/{len(queue)} {counts} s/company={el / done:.1f}")

    def pdf_task(code: str, res: CompanyResult) -> CompanyResult:
        try:
            fetch_pdf(client, res.doc, res)
        except Blocked as e:
            # the marker at once, from this thread, before the main thread gets to it
            mark(getattr(e, "url", None), getattr(e, "status", None), getattr(e, "reason", None), e)
            raise
        if res.status in ("ok", "extract_failed"):
            extra = doc_note(res.doc, today)
            res.extract_note = f"{res.extract_note};{extra}" if res.extract_note else extra
        return res

    stop: str | None = None
    try:
        for code in queue:
            if stop or halt.is_set():
                break
            stop = collect(False)
            while not stop and len(in_flight) >= workers * 2:
                stop = collect(True, RESULT_POLL_S)
            if stop:
                break
            if batch.companies >= batch_size:
                try:
                    flush(FLUSH_WAIT_S)
                except store.StoreLocked:
                    log("store locked by another process; keeping the batch")
            at_of[code] = at = store.now_utc()
            if batch.started_at is None:
                batch.started_at = at
            summary["attempted"] += 1
            res = CompanyResult(code=code)
            try:
                rows = list_reports(client, code, res)
            except Blocked as e:
                mark(getattr(e, "url", None), getattr(e, "status", None), getattr(e, "reason", None), e)
                stop = "blocked"
                break
            except Halted:
                stop = stop or "halted"
                break
            except (OSError, http.client.HTTPException) as e:
                res.exc = e
                rows = None
            finally:
                summary["list_requests"] += 1
            if rows is not None:
                path, dig = store.save_raw(cfg, SOURCE_ID, f"ar_list-{code}", res.listing_raw or b"")
                batch.listings.append({"code": code, "raw_path": path, "raw_sha256": dig,
                                       "raw_bytes": len(res.listing_raw or b""), "fetched_at": at.isoformat(),
                                       "rows": len(rows)})
            if rows is None:
                stop = handle(code, res, None)
                progress()
                continue
            doc = select_latest(rows)
            if doc is None:
                res.status, res.http_status, res.note = "no_annual_report", 200, "no_annual_report"
                stop = handle(code, res, None)
                progress()
                continue
            res.doc = doc
            prior = stored.get((code, doc["url"]))
            if prior is not None and not refresh:
                res.status = "skipped_unchanged" if prior == "ok" else "skipped_failed"
                handle(code, res, None)
                continue
            in_flight[pool.submit(pdf_task, code, res)] = code
            progress()
        # drain
        deadline = time.monotonic() + (DRAIN_MAX_S if (stop or halt.is_set()) else PDF_DEADLINE_S * 2 + 60)
        while in_flight and time.monotonic() < deadline:
            r = collect(True, RESULT_POLL_S)
            stop = stop or r
        summary["abandoned_in_flight"] = len(in_flight)
    except KeyboardInterrupt:
        halt.set()
        deadline = time.monotonic() + DRAIN_MAX_S
        try:
            while in_flight and time.monotonic() < deadline:
                collect(True, RESULT_POLL_S)
        except (KeyboardInterrupt, Exception):  # noqa: BLE001 - a second Ctrl-C: flush what we have
            pass
        summary["abandoned_in_flight"] = len(in_flight)
        pool.shutdown()
        summary["stopped_reason"] = "interrupted"
        _final_flush(flush, summary, log)
        return finish("interrupted")
    finally:
        pool.shutdown()
    if stop == "blocked":
        halt.set()
    summary["stopped_reason"] = stop if stop != "halted" else "blocked"
    summary["wall_s"] = round(time.monotonic() - t_start, 2)
    if done:
        summary["seconds_per_company"] = round(summary["wall_s"] / done, 2)
    locked = _final_flush(flush, summary, log)
    if summary["stopped_reason"] == "blocked":
        return finish("blocked", summary["blocked_reason"] or "blocked")
    if summary["stopped_reason"] == "consecutive_errors":
        return finish("stopped_errors")
    if locked:
        return finish("store_locked")
    return finish("ok")


def _final_flush(flush: Callable[..., None], summary: dict, log: Callable[[str], None]) -> bool:
    """Final flush; True when the store stayed locked (the batch is lost, the run says so)."""
    try:
        flush(FINAL_WAIT_S)
        return False
    except store.StoreLocked:
        log("store locked; final batch not written")
        summary["stopped_reason"] = summary["stopped_reason"] or "store_locked"
        return True
