"""SEC EDGAR adapter: TradingView US lines -> CIK -> latest annual report -> business section (L2 evidence for Jev).

Sources (provenance.py):
- 'sec_tickers' (official-open): company_tickers_exchange.json and the data.sec.gov submissions API.
- 'sec_filing_text' (official-private): issuer-authored 10-K Item 1 / 20-F Item 4 text. The text is kept in local
  files under <home>/docs/sec/<cik>/ and never shipped; only metadata goes into `documents`.

Rules implemented here:
- SEC fair access: a declared User-Agent is mandatory (cfg.sec_user_agent()). It is sent per request as a header and
  NEVER printed, logged, stored in snapshots/request_json or notes: every recorded request shows '<sec-user-agent>'.
- www.sec.gov and data.sec.gov share one limiter (rate_key 'sec.gov'), min interval 0.15 s (~6.7 req/s < 10 req/s).
- Client choice: sync() uses the client it is given (headers={'User-Agent': ua}, rate_key=RATE_KEY) and only raises
  the client's min_interval_s to DEFAULT_MIN_INTERVAL_S if it is lower. Pass client=None to have sync() build an
  http.Client from cfg with DEFAULT_MIN_INTERVAL_S.
- Full primary documents are NOT stored: only raw_sha256/raw_bytes plus the extracted section text file.
- No DB connection is held during network I/O: every DB touch is a short store.session() per batch.
- http.Blocked stops the whole run immediately and writes a cooldown marker (guard.mark_blocked) before any DB
  write; 5 consecutive errors stop it; Ctrl-C flushes and records 'interrupted'. Missing values stay NULL.
- Redaction happens BEFORE truncation (a cut through the UA would leave an unmatched partial copy), and also covers
  the JSON-escaped form of a non-ASCII UA.
- A mid-run flush that finds the store locked keeps the batch and retries after another batch_size CIKs; after
  MAX_LOCKED_FLUSHES failed attempts (or MAX_PENDING_CIKS pending) the run stops with 'store_locked'.
"""
from __future__ import annotations

import datetime as dt
import html as htmlmod
import http.client
import json
import re
import sys
import time
import uuid
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .. import guard, store
from ..config import Config
from ..config import redact as redact_secrets
from ..http import Blocked, Client

TICKERS_SOURCE = "sec_tickers"
SOURCE_ID = "sec_filing_text"
TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SUBMISSIONS_PAGE_URL = "https://data.sec.gov/submissions/{name}"   # older pages listed in filings.files
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{doc}"
RATE_KEY = "sec.gov"
DEFAULT_MIN_INTERVAL_S = 0.15
ANNUAL_FORMS = ("10-K", "20-F", "40-F")
TRANSITION_FORMS = {"10-KT": "10-K", "20-FT": "20-F"}   # transition reports: same structure as the base form
MAX_OLDER_PAGES = 3          # older submissions pages searched when `recent` holds no annual report
STALE_YEARS = 2              # extract_note gets 'stale:<years>' when the report is older than this
UA_PLACEHOLDER = "<sec-user-agent>"
ID_TYPE = "sec_cik"
EXTRACTOR_VERSION = "sec_edgar-v2"

MIN_SECTION_CHARS = 2_000
MAX_SECTION_CHARS = 600_000
MAX_DOC_BYTES = 128 * 1024 * 1024
DOC_DEADLINE_S = 600.0       # http.Client total wall-clock time per primary-document attempt (default is 120 s)
MAX_CONSECUTIVE_ERRORS = 5
FLUSH_WAIT_S = 120.0         # mid-run flush: wait this long for another process's lock, then keep the batch
FINAL_WAIT_S = 600.0         # final flush / run journal: wait longer, then raise StoreLocked
MAX_LOCKED_FLUSHES = 5       # consecutive locked mid-run flushes before the run stops ('store_locked')
MAX_PENDING_CIKS = 500       # pending (unflushed) CIKs before the run stops ('store_locked')

# TradingView exchange codes that denote US venues (NYSE ARCA / ARCA, CBOE / BATS spelled both ways).
US_VENUES = frozenset({"NASDAQ", "NYSE", "AMEX", "NYSE ARCA", "NYSEARCA", "ARCA", "CBOE", "BATS", "OTC"})
# SEC exchange label -> TradingView venue family, used only to break ties between several CIK candidates.
_SEC_VENUE = {"NASDAQ": "NASDAQ", "NYSE": "NYSE", "OTC": "OTC", "CBOE": "CBOE"}
_TV_VENUE = {"NASDAQ": "NASDAQ", "NYSE": "NYSE", "AMEX": "NYSE", "NYSE ARCA": "NYSE", "NYSEARCA": "NYSE",
             "ARCA": "NYSE", "CBOE": "CBOE", "BATS": "CBOE", "OTC": "OTC"}

CRAWL_STATUSES = ("ok", "no_annual_filing", "form_not_supported", "extract_failed", "error", "blocked")


# =========================================================================== tickers -> CIK


def parse_tickers(payload: Any) -> list[dict]:
    """company_tickers_exchange.json ({'fields': [...], 'data': [[...]]}) -> [{cik, name, ticker, exchange}].

    Also accepts an already-parsed list of dicts or of [cik, name, ticker, exchange] lists.
    """
    if isinstance(payload, (bytes, str)):
        payload = json.loads(payload)
    if isinstance(payload, Mapping) and "data" in payload:
        fields = list(payload.get("fields") or ["cik", "name", "ticker", "exchange"])
        rows = [dict(zip(fields, r)) for r in payload["data"]]
    elif isinstance(payload, Mapping):   # company_tickers.json shape {"0": {"cik_str":..,"ticker":..,"title":..}}
        rows = [{"cik": v.get("cik_str", v.get("cik")), "name": v.get("title", v.get("name")),
                 "ticker": v.get("ticker"), "exchange": v.get("exchange")} for v in payload.values()]
    else:
        rows = [r if isinstance(r, Mapping) else dict(zip(["cik", "name", "ticker", "exchange"], r))
                for r in (payload or [])]
    out = []
    for r in rows:
        try:
            cik = int(r.get("cik"))
        except (TypeError, ValueError):
            continue
        ticker = r.get("ticker")
        if not isinstance(ticker, str) or not ticker.strip():
            continue
        out.append({"cik": cik, "name": r.get("name"), "ticker": ticker.strip(), "exchange": r.get("exchange")})
    return out


def normalise_ticker(symbol: str) -> str:
    """Class-separator-insensitive ticker: 'BRK.B', 'BRK/B', 'BRK B', 'brk-b' -> 'BRK-B'."""
    s = (symbol or "").strip().upper()
    s = re.sub(r"[.\s/_]+", "-", s)
    return re.sub(r"-{2,}", "-", s).strip("-")


def _split_sid(sec: Mapping[str, Any]) -> tuple[str, str]:
    sid = str(sec.get("security_id") or "")
    exch, _, sym = sid.partition(":")
    return (str(sec.get("exchange") or exch).strip().upper(), str(sec.get("symbol") or sym).strip())


def map_securities_to_cik(securities: Sequence[Mapping[str, Any]], tickers_rows: Any, *,
                          report: dict | None = None) -> list[tuple[str, int, str]]:
    """Map TradingView lines on US venues to SEC CIKs by ticker. Returns [(security_id, cik, method)].

    - Only exchanges in US_VENUES are considered (NASDAQ, NYSE, AMEX, NYSE ARCA/ARCA, CBOE/BATS, OTC).
    - method 'ticker_exact' when the TradingView symbol equals the SEC ticker (case-insensitive), else
      'ticker_class_normalised' when they agree after class-separator normalisation (TV 'BRK.B' vs SEC 'BRK-B').
    - Exactly one CIK per security. If a ticker maps to several CIKs, a candidate on the same venue family wins;
      otherwise the security is left unmapped and listed in report['ambiguous'].
    `report` (optional dict) receives counts: considered, non_us, unmatched, ambiguous (list of dicts).
    """
    rows = parse_tickers(tickers_rows)
    exact: dict[str, dict[int, dict]] = {}
    norm: dict[str, dict[int, dict]] = {}
    for r in rows:
        exact.setdefault(r["ticker"].upper(), {})[r["cik"]] = r
        norm.setdefault(normalise_ticker(r["ticker"]), {})[r["cik"]] = r
    rep = {"considered": 0, "non_us": 0, "unmatched": 0, "ambiguous": []}
    out: list[tuple[str, int, str]] = []
    seen: set[str] = set()
    for sec in securities:
        sid = str(sec.get("security_id") or "")
        exch, sym = _split_sid(sec)
        if not sid or sid in seen:
            continue
        seen.add(sid)
        if exch not in US_VENUES or not sym:
            rep["non_us"] += 1
            continue
        rep["considered"] += 1
        cands, method = exact.get(sym.upper()), "ticker_exact"
        if not cands:
            cands, method = norm.get(normalise_ticker(sym)), "ticker_class_normalised"
        if not cands:
            rep["unmatched"] += 1
            continue
        if len(cands) > 1:
            fam = _TV_VENUE.get(exch)
            same = [c for c, r in cands.items() if _SEC_VENUE.get(str(r.get("exchange") or "").upper()) == fam]
            if len(same) == 1:
                out.append((sid, same[0], method))
                continue
            rep["ambiguous"].append({"security_id": sid, "ticker": sym, "ciks": sorted(cands)})
            continue
        out.append((sid, next(iter(cands)), method))
    if report is not None:
        report.update(rep)
    return out


# =========================================================================== submissions -> latest annual filing


def form_base(form: Any) -> str:
    """'10-K/A' -> '10-K', '10-KT' -> '10-K', '20-FT/A' -> '20-F', '40-F' -> '40-F'."""
    f = str(form or "").upper().strip().removesuffix("/A")
    return TRANSITION_FORMS.get(f, f)


def latest_annual_filing(submissions_json: Mapping[str, Any], forms: Sequence[str] = ANNUAL_FORMS, *,
                         notes: list[str] | None = None, as_of: dt.date | None = None) -> dict | None:
    """Latest annual report in filings.recent: {form, base_form, accession, primary_document, filing_date,
    report_date, amendment, flags}.

    Transition reports (10-KT / 20-FT) count as their base form. Originals only; an amendment ('10-K/A' etc.) is
    returned only when no original of any requested form exists in `recent` (then 'amendment': True). None when
    nothing qualifies; a reason is appended to `notes` ('no_annual_filing_in_recent' when older filing pages exist
    but were not searched, else 'no_annual_filing'). `flags` lists caveats for extract_note:
    'newer_annual_form_ignored:<form>' (a newer annual report of a form outside `forms`, e.g. a 40-F) and
    'stale:<years>' (report/filing date more than STALE_YEARS years before `as_of`, default today UTC).
    The same function reads an older submissions page when given {'filings': {'recent': <page>}}.
    """
    filings = (submissions_json or {}).get("filings") or {}
    recent = filings.get("recent") or {}
    forms_l = list(recent.get("form") or [])
    n = len(forms_l)

    def col(name: str) -> list:
        v = list(recent.get(name) or [])
        return v + [None] * (n - len(v))

    acc, fdate, rdate, pdoc = col("accessionNumber"), col("filingDate"), col("reportDate"), col("primaryDocument")
    wanted = {form_base(f) for f in forms}
    all_annual = set(ANNUAL_FORMS) | set(TRANSITION_FORMS.values())
    originals, amendments, excluded = [], [], []
    for i, form in enumerate(forms_l):
        raw = str(form or "").upper().strip()
        base = form_base(raw)
        if base in wanted:
            (amendments if raw.endswith("/A") else originals).append(i)
        elif base in all_annual and not raw.endswith("/A") and acc[i]:
            excluded.append(i)
    pool, is_amend = (originals, False) if originals else (amendments, True)
    pool = [i for i in pool if acc[i] and pdoc[i]]
    if not pool:
        if notes is not None:
            notes.append("no_annual_filing_in_recent" if filings.get("files") else "no_annual_filing")
        return None
    # newest filing date first; ties keep the API order (which is newest first)
    best = min(pool, key=lambda i: (_neg_date(fdate[i]), i))
    flags: list[str] = []
    newer = [i for i in excluded if _neg_date(fdate[i]) < _neg_date(fdate[best])]
    if newer:
        flags.append(f"newer_annual_form_ignored:{str(forms_l[min(newer, key=lambda i: _neg_date(fdate[i]))]).strip()}")
    ref = _date_or_none(rdate[best]) or _date_or_none(fdate[best])
    today = as_of or dt.datetime.now(dt.timezone.utc).date()
    if ref is not None and (today - ref).days > STALE_YEARS * 365.25:
        flags.append(f"stale:{int((today - ref).days // 365.25)}")
    form = str(forms_l[best]).strip()
    return {"form": form, "base_form": form_base(form), "accession": acc[best],
            "primary_document": pdoc[best], "filing_date": fdate[best] or None, "report_date": rdate[best] or None,
            "amendment": is_amend, "flags": flags}


def _date_or_none(s: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def _neg_date(s: Any) -> int:
    try:
        return -dt.date.fromisoformat(str(s)[:10]).toordinal()
    except (TypeError, ValueError):
        return 0


def archive_url(cik: int, accession: str, doc: str) -> str:
    return ARCHIVE_URL.format(cik=int(cik), acc_nodash=str(accession).replace("-", ""), doc=doc)


# =========================================================================== HTML -> text

_BLOCK_TAGS = frozenset("""p div br tr li ul ol table tbody thead tfoot h1 h2 h3 h4 h5 h6 section article header
footer blockquote pre hr dl dt dd center title body html form address caption""".split())
_CELL_TAGS = frozenset({"td", "th"})
_DROP_TAGS = frozenset({"script", "style", "head", "ix:header", "noscript", "template", "xml"})
_VOID_TAGS = frozenset("area base br col embed hr img input link meta param source track wbr".split())
_HIDDEN_RE = re.compile(r"display\s*:\s*none", re.I)


class _TextParser(HTMLParser):
    """Stdlib fallback: collect visible text; drop script/style/ix:header and display:none subtrees."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_tag: str | None = None
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._skip_tag is not None:
            if tag == self._skip_tag and tag not in _VOID_TAGS:
                self._skip_depth += 1
            return
        if tag in _DROP_TAGS or (tag not in _VOID_TAGS and _HIDDEN_RE.search(dict(attrs).get("style") or "")):
            if tag not in _VOID_TAGS:
                self._skip_tag, self._skip_depth = tag, 1
            return
        self._brk(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_tag is None:
            self._brk(tag.lower())

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth <= 0:
                    self._skip_tag = None
            return
        self._brk(tag)

    def _brk(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n" if tag == "br" else "\n\n")
        elif tag in _CELL_TAGS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._skip_tag is None:
            self.parts.append(_flat(data))


def _flat(data: str) -> str:
    """Source line breaks inside text nodes are plain whitespace in HTML; only markup creates breaks."""
    return data.replace("\r", " ").replace("\n", " ") if ("\n" in data or "\r" in data) else data


def _decode(html_bytes: bytes | str) -> str:
    if isinstance(html_bytes, str):
        return html_bytes
    head = html_bytes[:2048].decode("ascii", "replace").lower()
    m = re.search(r"""(?:encoding|charset)\s*=\s*["']?([a-z0-9_\-]+)""", head)
    for enc in ([m.group(1)] if m else []) + ["utf-8"]:
        try:
            return html_bytes.decode("utf-8" if enc in ("ascii", "us-ascii") else enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return html_bytes.decode("cp1252", "replace")


def _html_parts_stdlib(text: str) -> list[str]:
    p = _TextParser()
    p.feed(text)
    p.close()
    return p.parts


def _html_parts_lxml(text: str) -> list[str] | None:
    try:
        import lxml.html  # optional speed-up
        from lxml import etree
    except ImportError:
        return None
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
    try:
        root = lxml.html.document_fromstring(text)
    except (etree.ParserError, ValueError):
        return None
    parts: list[str] = []

    def walk(el) -> None:
        tag = el.tag.lower() if isinstance(el.tag, str) else None
        if tag is None:   # comment / processing instruction: skip it but keep its tail
            if el.tail:
                parts.append(_flat(el.tail))
            return
        hidden = tag in _DROP_TAGS or (tag not in _VOID_TAGS and _HIDDEN_RE.search(el.get("style") or ""))
        if not hidden:
            brk = "\n" if tag == "br" else ("\n\n" if tag in _BLOCK_TAGS else (" " if tag in _CELL_TAGS else ""))
            parts.append(brk)
            if el.text:
                parts.append(_flat(el.text))
            for child in el:
                walk(child)
            if tag != "br":
                parts.append(brk)
        if el.tail:
            parts.append(_flat(el.tail))

    walk(root)
    return parts


_WS_RE = re.compile(r"[ \t\r\f\v     ​  　]+")


def _normalise_text(raw: str) -> str:
    raw = raw.replace("­", "")
    lines = [_WS_RE.sub(" ", ln).strip() for ln in raw.split("\n")]
    out: list[str] = []
    blank = 0
    for ln in lines:
        if not ln:
            blank += 1
            continue
        if out:
            out.append("\n\n" if blank >= 2 else "\n")
        out.append(ln)
        blank = 0
    return "".join(out).strip()


def html_to_text(html_bytes: bytes | str, *, backend: str = "auto") -> str:
    """Visible text of an (inline-XBRL) 10-K/20-F HTML document.

    Drops script/style/head, the ix:header block and every display:none subtree; block elements become paragraph
    breaks (blank line), <br> a line break, table cells a space (so 'ITEM 1.' | 'Business' reads 'ITEM 1. Business');
    entities are unescaped once (by the parser; never a second time, so a literal '&amp;lt;' stays '&lt;') and
    whitespace (incl. NBSP) normalised. backend: 'auto' (lxml if importable, else
    stdlib html.parser), 'lxml' or 'stdlib'.
    """
    text = _decode(html_bytes)
    parts = None
    if backend in ("auto", "lxml"):
        parts = _html_parts_lxml(text)
        if parts is None and backend == "lxml":
            raise RuntimeError("lxml backend unavailable")
    if parts is None:
        parts = _html_parts_stdlib(text)
    return _normalise_text("".join(parts))


def html_backend() -> str:
    try:
        import lxml.html  # noqa: F401
        return "lxml"
    except ImportError:
        return "stdlib"


# =========================================================================== section extraction

_SEP = r"[\s.:\-–—‒―]*"                 # period / colon / hyphen / en/em dash / spaces
_LS = r"(?:^|\n)[ \t]*(?:part[ \t]+i[ \t]*[,.:\-–—]?[ \t]*)?"   # line start, optional 'PART I' prefix


def _rx(body: str) -> re.Pattern:
    return re.compile(_LS + body, re.I)


_SECTION_RULES: dict[str, dict] = {
    "10-K": {
        "section": "item1",
        "start": [_rx(r"items?" + _SEP + r"1" + _SEP + r"(?:(?:and|&)" + _SEP + r"2" + _SEP + r")?"
                      r"(?:(?:description" + _SEP + r"of|our)" + _SEP + r")?business\b")],
        "end": [("item1a", _rx(r"item" + _SEP + r"1" + r"[ \t]*a\b")),
                ("item1a", _rx(r"item" + _SEP + r"1" + _SEP + r"a" + _SEP + r"risk\s+factors")),
                ("item1b", _rx(r"item" + _SEP + r"1" + r"[ \t]*b\b")),
                ("item1c", _rx(r"item" + _SEP + r"1" + r"[ \t]*c\b")),
                ("item2", _rx(r"items?" + _SEP + r"2" + _SEP + r"(?:description" + _SEP + r"of" + _SEP +
                              r")?propert")),
                ("item3", _rx(r"items?" + _SEP + r"3" + _SEP + r"legal"))],
    },
    "20-F": {
        "section": "item4",
        # 'Information on the Company', or on the filer by name ('ITEM 4. INFORMATION ON CHECK POINT', 'ITEM 4:
        # Information on Allot'): the heading's own line, never a cross-reference inside a sentence
        "start": [_rx(r"item" + _SEP + r"4" + _SEP + r"information" + _SEP + r"on" + _SEP + r"the" + _SEP +
                      r"company\b"),
                  _rx(r"item" + _SEP + r"4" + _SEP + r"information[ \t]+on[ \t]+[A-Za-z][^\n\u201d\"]{0,60}(?=\n|$)")],
        # 'Item 4A' ends Item 4 only as 'Unresolved Staff Comments' (or marked not applicable / none): some
        # filers label the Item 4 sub-headings 'Item 4A. History...' / 'Item 4B. Business Overview'.
        "end": [("item4a", _rx(r"item" + _SEP + r"4" + _SEP + r"a" + _SEP + r"unresolved")),
                ("item4a", _rx(r"item" + _SEP + r"4" + _SEP + r"a" + _SEP +
                               r"(?:not\s+applicable|n/?a\b|none\b)")),
                ("item5", _rx(r"item" + _SEP + r"5" + _SEP + r"operating"))],
    },
}
SECTION_FOR_FORM = {"10-K": "item1", "20-F": "item4"}


def section_for_form(form: str) -> str | None:
    return SECTION_FOR_FORM.get(str(form or "").upper().removesuffix("/A"))


def extract_section(text: str, form: str) -> tuple[str | None, str]:
    """10-K -> Item 1 (Business) up to the next item heading (1A, else 1B/1C/2/3); 20-F -> Item 4 (Information on
    the Company) up to Item 4A (else Item 5). 40-F -> (None, 'form_40F_aif_in_exhibit_not_supported').

    Headings must start a line (optional 'PART I' prefix); period/colon/dash/spacing/case variants and the combined
    'Items 1 and 2. Business and Properties', 'Item 1. Description of Business' and 'Item 1. Our Business' headings
    are accepted. Every start occurrence is paired with the nearest following end heading; spans shorter than 2,000
    chars (table of contents, cross-reference index) or opening on a TOC-like block are rejected; when several starts
    share one end the latest wins; the longest remaining span wins. Result bounded to 2,000..600,000 chars, otherwise (None, note).
    Note on success: 'ok:end=<boundary>' (e.g. 'ok:end=item1a').
    """
    f = str(form or "").upper().strip().removesuffix("/A")
    if f == "40-F":
        return None, "form_40F_aif_in_exhibit_not_supported"
    rules = _SECTION_RULES.get(f)
    if rules is None:
        return None, f"form_not_supported:{f or 'none'}"
    if not text:
        return None, "empty_text"
    starts = sorted({m.start() + (1 if text[m.start()] == "\n" else 0)
                     for rx in rules["start"] for m in rx.finditer(text)})
    if not starts:
        return None, "start_heading_not_found"
    ends: list[tuple[int, str]] = sorted(
        (m.start() + (1 if text[m.start()] == "\n" else 0), name) for name, rx in rules["end"] for m in rx.finditer(text))
    longest_any = 0
    no_end = 0
    by_end: dict[int, tuple[int, str]] = {}   # end -> (latest qualifying start, boundary name)
    for s in starts:
        nxt = next(((e, name) for e, name in ends if e > s), None)
        if nxt is None:
            no_end += 1
            continue
        length = nxt[0] - s
        longest_any = max(longest_any, length)
        if MIN_SECTION_CHARS <= length <= MAX_SECTION_CHARS and not _toc_like(text, s):
            # several starts sharing one end: the latest one is the body heading (earlier ones are TOC /
            # cross-reference lines whose own end heading was spelled differently)
            if nxt[0] not in by_end or s > by_end[nxt[0]][0]:
                by_end[nxt[0]] = (s, nxt[1])
    best: tuple[int, int, str] | None = None
    for e, (s, name) in by_end.items():
        if best is None or e - s > best[1] - best[0]:
            best = (s, e, name)
    if best is None:
        if longest_any > MAX_SECTION_CHARS:
            return None, f"section_too_long:{longest_any}"
        if longest_any == 0 and no_end:
            return None, "end_heading_not_found"
        return None, f"section_too_short_or_toc_only:{longest_any}"
    section = _trim_trailing(text[best[0]:best[1]])
    if len(section) < MIN_SECTION_CHARS:
        return None, f"section_too_short_or_toc_only:{len(section)}"
    return section, f"ok:end={best[2]}"


_TOC_ROW_RE = re.compile(
    r"^(?:(?:items?[ \t]*)?\d{1,2}[a-c]?\."                                   # bare item number: '1A.'
    r"|(?:items?[ \t]*)?\d{1,2}[a-c]?\.?[ \t]+[a-z][^\n]{0,100}?[ \t.]+\d{1,3}"   # 'Item 1. Business 3'
    r"|(?:items?[ \t]*)?\d{1,2}[a-c]?\.[a-z][^\n]{0,100}?[ \t.]+\d{1,3})$", re.I)
TOC_WINDOW = 1_500


def _toc_like(text: str, start: int) -> bool:
    """True when a table-of-contents block sits right after `start`: 2+ lines within TOC_WINDOW chars that are a
    bare item number ('1A.') or 'item + title + page number' ('Item 1. Business 3', '1A. Risk Factors 10')."""
    lines = [ln.strip() for ln in text[start:start + TOC_WINDOW].split("\n")]
    return sum(1 for ln in lines if ln and _TOC_ROW_RE.match(ln)) >= 2


_TRAILER_RE = re.compile(r"(?:\n\s*(?:table\s+of\s+contents|index|page\s*\d+|\d{1,4}|[ivxlc]{1,6}"
                         r"|part\s+i{1,3})[ \t]*)+\s*$", re.I)


def _trim_trailing(section: str) -> str:
    """Drop page-footer debris (page numbers, 'Table of Contents', 'PART I') left just before the end heading."""
    return _TRAILER_RE.sub("", "\n" + section.strip()).strip()


# =========================================================================== short description

_FLS_RE = re.compile(r"forward[\s\-]*looking\s+statement|safe\s+harbor|private\s+securities\s+litigation", re.I)
# cross-reference paragraphs ('For a breakdown of revenues ... please see Item 5 ...') say nothing about the business
_XREF_RE = re.compile(r"^(?:for\s+(?:a|an|further|more|additional)\b.{0,250}?\b(?:see|refer\s+to)\b|(?:please\s+)?see\s+item\b"
                      r"|refer\s+to\s+item\b)", re.I)
_SKIP_PARA_RE = re.compile(
    r"^(?:table\s+of\s+contents|index|page\s*\d*|\d{1,4}|[ivxlc]{1,6}|part\s+i+\b.*|items?\s+\d+[a-z]?\b.*)$", re.I)


def _is_heading(p: str) -> bool:
    if _SKIP_PARA_RE.match(p):
        return True
    if len(p) < 120 and not re.search(r"[.!?:;][\"'”)]?$", p):
        return True
    letters = [c for c in p if c.isalpha()]
    return bool(letters) and len(p) < 300 and sum(c.isupper() for c in letters) / len(letters) > 0.8


_BIZ_OVERVIEW_RE = re.compile(r"(?:^|\n)[ \t]*(?:item[ \t]*)?(?:4[ \t]*[.:\-–—]?[ \t]*)?(?:b[ \t]*[.:\-–—)]?[ \t]*)?"
                              r"business[ \t]+overview\b[^\n]*", re.I)


def description_source(section_text: str | None, form: str) -> str | None:
    """Text to take the short description from: for a 20-F the part from the 'B. Business Overview' sub-heading
    (Item 4.A is company history), falling back to the whole section; other forms: the whole section."""
    if not section_text or form_base(form) != "20-F":
        return section_text
    m = _BIZ_OVERVIEW_RE.search(section_text)
    if m:
        rest = section_text[m.end():]
        if short_description(rest):
            return rest
    return section_text


def short_description(section_text: str | None, max_chars: int = 1500) -> str:
    """First meaningful paragraphs of a business section, at most max_chars.

    Skips headings (item lines, short lines without terminal punctuation, all-caps lines), page numbers,
    'Table of Contents', forward-looking-statement boilerplate and cross-reference paragraphs ('... see Item 5'). Cuts at a sentence end when possible.
    Returns '' when nothing qualifies.
    """
    if not section_text:
        return ""
    paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n|\n", section_text)]
    picked: list[str] = []
    total = 0
    for p in paras:
        if not p or _is_heading(p) or _FLS_RE.search(p) or _XREF_RE.search(p) or len(p) < 60:
            continue
        add = len(p) + (1 if picked else 0)
        if total + add <= max_chars:
            picked.append(p)
            total += add
            if total >= max_chars * 0.8:
                break
            continue
        room = max_chars - total - (1 if picked else 0)
        if room > 200 or not picked:
            cut = p[:room]
            m = list(re.finditer(r"[.!?][\"'”)]?(?=\s)", cut))
            if m and m[-1].end() > room * 0.4:
                cut = cut[:m[-1].end()]
            else:
                cut = cut.rstrip() if len(p) <= room else cut.rsplit(" ", 1)[0].rstrip() + "…"
            picked.append(cut)
        break
    return "\n".join(picked)[:max_chars].strip()


# =========================================================================== sync


class SecUserAgentMissing(RuntimeError):
    """SEC requires a declared User-Agent ('<name> <email>')."""


def _redactor(ua: str):
    """Replace the UA (verbatim or JSON-escaped) by UA_PLACEHOLDER. Always redact BEFORE truncating."""
    def redact(s: Any) -> Any:
        return redact_secrets(s, [ua], UA_PLACEHOLDER)
    return redact


def _err_note(redact, prefix: str, e: BaseException, limit: int = 200) -> str:
    """'<prefix><Type>: <message>' redacted first, then cut to `limit` chars of message."""
    return redact(f"{prefix}{type(e).__name__}: {e}")[:len(prefix) + limit]


def _req_record(urls: Iterable[str] | str) -> dict:
    rec: dict[str, Any] = {"method": "GET", "headers": {"User-Agent": UA_PLACEHOLDER}, "rate_key": RATE_KEY}
    if isinstance(urls, str):
        rec["url"] = urls
    else:
        rec["urls"] = list(urls)
    return rec


def text_path_for(cfg: Config, cik: int, accession: str, section: str) -> Path:
    return Path(cfg.home) / "docs" / "sec" / str(int(cik)) / f"{accession}-{section}.txt"


class _Batch:
    def __init__(self) -> None:
        self.subs: list[dict] = []       # submissions manifest entries
        self.docs: list[dict] = []       # documents rows (dicts) waiting for the filing snapshot id
        self.descs: list[dict] = []
        self.states: list[list[Any]] = []
        self.started_at: dt.datetime | None = None
        self.duration_s = 0.0
        self.ciks = 0

    def __len__(self) -> int:
        return len(self.states) + len(self.subs)


_DOC_COLS = ["doc_id", "security_id", "company_key", "source_id", "cik", "form", "section", "accession",
             "filing_date", "report_date", "url", "raw_sha256", "raw_bytes", "text_path", "text_sha256", "text_chars",
             "extractor", "extract_note", "fetched_at", "snapshot_id"]
_DESC_COLS = ["security_id", "source_id", "company_key", "text", "text_sha256", "lang", "source_url", "fetched_at",
              "match_method", "match_score", "snapshot_id"]
_STATE_COLS = ["source_id", "security_id", "status", "http_status", "attempts", "last_attempt_at", "note"]


def _date(s: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def _flush(cfg: Config, batch: _Batch, attempts: dict[str, int], redact, note: str | None = None, *,
           wait_s: float = FINAL_WAIT_S) -> int:
    """One short write session per batch: snapshots + documents + descriptions + crawl_state, atomically.

    Raises store.StoreLocked when the lock outlasts `wait_s`; the batch is untouched then (the caller keeps it)."""
    if not len(batch):
        return 0
    snaps = 0
    sub_manifest = json.dumps(batch.subs, sort_keys=True).encode() if batch.subs else None
    doc_manifest = (json.dumps([{k: (v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v)
                                 for k, v in d.items()} for d in batch.docs], sort_keys=True).encode()
                    if batch.docs else None)
    with store.session(cfg, wait_s=wait_s) as con:
        # manifests are written only once the session is open, so a locked attempt leaves no orphan raw file
        sub_path = sub_digest = doc_path = doc_digest = None
        if sub_manifest is not None:
            sub_path, sub_digest = store.save_raw(cfg, TICKERS_SOURCE, "submissions_batch", sub_manifest)
        if doc_manifest is not None:
            doc_path, doc_digest = store.save_raw(cfg, SOURCE_ID, "filing_batch", doc_manifest)
        con.begin()
        try:
            if batch.subs:
                store.record_snapshot(
                    con, source_id=TICKERS_SOURCE, kind="submissions_batch",
                    request=_req_record([s["url"] for s in batch.subs]), raw_path=sub_path, raw_sha256=sub_digest,
                    raw_bytes=len(sub_manifest), rows=len(batch.subs), duration_s=round(batch.duration_s, 3),
                    note=redact(note), fetched_at=batch.started_at)
                snaps += 1
            snap_id = None
            if batch.docs:
                snap_id = store.record_snapshot(
                    con, source_id=SOURCE_ID, kind="filing_batch",
                    request=_req_record([d["url"] for d in batch.docs]), raw_path=doc_path, raw_sha256=doc_digest,
                    raw_bytes=len(doc_manifest), rows=len(batch.docs), duration_s=round(batch.duration_s, 3),
                    note=redact(note), fetched_at=batch.started_at)
                snaps += 1
            store.upsert_many(con, "documents", _DOC_COLS,
                              [[d.get(c) if c != "snapshot_id" else snap_id for c in _DOC_COLS] for d in batch.docs])
            store.upsert_many(con, "descriptions", _DESC_COLS,
                              [[d.get(c) if c != "snapshot_id" else snap_id for c in _DESC_COLS]
                               for d in batch.descs])
            store.upsert_many(con, "crawl_state", _STATE_COLS,
                              [[r[0], r[1], r[2], r[3], r[4], r[5], redact(r[6])] for r in batch.states])
            con.commit()
        except BaseException:
            con.rollback()
            raise
    for r in batch.states:
        attempts[r[1]] = r[4]
    return snaps


def _load_lines(con, only_universe: bool) -> list[dict]:
    placeholders = ",".join("?" for _ in US_VENUES)
    if only_universe:
        sql = f"""SELECT security_id, exchange, symbol, company_key, market_cap_usd, tv_type FROM universe
                  WHERE upper(exchange) IN ({placeholders})"""
    else:
        sql = f"""SELECT s.security_id, s.exchange, s.symbol, s.company_key, lm.market_cap_usd, s.tv_type
                  FROM securities s LEFT JOIN latest_market lm USING (security_id)
                  WHERE s.active AND upper(s.exchange) IN ({placeholders})"""
    cols = ["security_id", "exchange", "symbol", "company_key", "market_cap_usd", "tv_type"]
    return [dict(zip(cols, r)) for r in con.execute(sql, sorted(US_VENUES)).fetchall()]


_PAGE_NAME_RE = re.compile(r"[A-Za-z0-9_\-]{1,80}\.json")


def _norm_codes(codes: Iterable[str] | str | None) -> set[str] | None:
    """{'NASDAQ:ABC', 'ABC'} per given code (security id and its ticker part), upper-case; None = no filter."""
    if codes is None:
        return None
    if isinstance(codes, str):
        codes = [c for c in re.split(r"[,\s]+", codes) if c]
    out: set[str] = set()
    for c in codes:
        c = str(c).strip().upper()
        if c:
            out |= {c, c.rsplit(":", 1)[-1]}
    return out


def _line_matches(ln: Mapping[str, Any], wanted: set[str]) -> bool:
    sid = str(ln.get("security_id") or "").upper()
    return sid in wanted or sid.rsplit(":", 1)[-1] in wanted or str(ln.get("symbol") or "").upper() in wanted


def sync(cfg: Config, client: Any = None, *, limit: int | None = None, forms: Sequence[str] = ANNUAL_FORMS,
         batch_size: int = 25, only_universe: bool = True, refresh: bool = False,
         progress_every: int = 100, codes: Iterable[str] | str | None = None,
         on_company: Callable[[list[str], str, str | None], None] | None = None) -> dict:
    """Map US universe lines to CIKs and pull the latest annual report's business section for each CIK.

    Steps: (1) require cfg.sec_user_agent() (SecUserAgentMissing otherwise); (2) GET the tickers file, save raw,
    snapshot, upsert identifiers (id_type 'sec_cik') and delete sec_cik rows of considered lines that no longer map;
    (3) queue distinct CIKs by market cap desc; (4) per CIK GET submissions (plus up to MAX_OLDER_PAGES older pages
    when `recent` holds no original annual report), pick the latest annual filing, skip it when the stored document
    already has that accession (unless refresh), GET the primary document, extract the section, write the text file,
    and per batch upsert documents/descriptions/crawl_state in one short session. Returns a summary dict whose
    'status' is ok | blocked | stopped_errors | interrupted | store_locked.
    `client`: any object with get(url, headers=, rate_key=, max_bytes=) (http.Client-compatible); None builds one.
    `codes` (security ids like 'NASDAQ:ABC' or tickers; None = all) keeps only the CIKs of matching lines in the queue
    (the tickers file is still fetched and every considered line mapped). `on_company(security_ids, status, note)` is
    called on this thread for every CIK whose status is recorded, and with 'skipped_unchanged' for a CIK whose stored
    document already has the latest accession (the on-demand fetch's per-company events); its errors are ignored.
    """
    wanted = _norm_codes(codes)
    ua = cfg.sec_user_agent()
    if not ua:
        raise SecUserAgentMissing(
            "SEC requires a declared User-Agent: set JEVSCREEN_SEC_USER_AGENT or write '<name> <email>' to "
            f"{Path(cfg.home) / 'sec_user_agent'}")
    redact = _redactor(ua)
    if client is None:
        client = Client(user_agent="jev-screen", min_interval_s=DEFAULT_MIN_INTERVAL_S, timeout_s=cfg.timeout_s)
    cur = getattr(client, "min_interval_s", None)
    if isinstance(cur, (int, float)) and cur < DEFAULT_MIN_INTERVAL_S:
        client.min_interval_s = DEFAULT_MIN_INTERVAL_S
    headers = {"User-Agent": ua, "Accept-Encoding": "identity"}
    batch_size = max(1, int(batch_size))
    forms = tuple(forms)
    run_id = f"sync-sec_edgar-{uuid.uuid4().hex[:12]}"
    requests_before = int(getattr(client, "requests_made", 0) or 0)
    counts = {s: 0 for s in CRAWL_STATUSES}
    summary: dict[str, Any] = {
        "run_id": run_id, "tickers_rows": 0, "us_lines": 0, "securities_mapped": 0, "ambiguous": [],
        "mapped_by_method": {}, "identifiers_removed": 0, "ciks_queued": 0, "ciks_attempted": 0,
        "skipped_unchanged": 0, "ok_by_form": {}, "not_supported": 0, "failures_by_note": {}, "older_pages": 0,
        "requests": 0, "bytes_downloaded": 0, "snapshots": 0, "stopped_reason": None, "blocked_at": None,
        "status": None}

    def log(msg: str) -> None:
        print(f"[sec_edgar] {redact(msg)}", file=sys.stderr, flush=True)

    def fetch(url: str, **kw):
        resp = client.get(url, headers=headers, rate_key=RATE_KEY, **kw)
        summary["bytes_downloaded"] += len(resp.body or b"")
        return resp

    def mark_blocked(url: str, e: Blocked) -> None:
        """Cooldown marker on disk first: it survives a final DB write that fails (StoreLocked, crash)."""
        guard.mark_blocked(cfg, guard.CMD_SYNC_SEC, url=getattr(e, "url", None) or url,
                           status=getattr(e, "status", None), reason=getattr(e, "reason", None))

    with store.session(cfg, wait_s=FINAL_WAIT_S) as con:
        con.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
                    [run_id, "sync sec_edgar", store.now_utc(), None, "running", 0, None])

    def finish(status: str, note: Any = None) -> None:
        summary["requests"] = int(getattr(client, "requests_made", 0) or 0) - requests_before
        summary["status"] = status
        payload = redact(json.dumps({"counts": counts, "stopped_reason": summary["stopped_reason"],
                                     **({"note": redact(note)} if note else {})}, default=str, ensure_ascii=False))
        with store.session(cfg, wait_s=FINAL_WAIT_S) as con:
            con.execute("UPDATE runs SET finished_at = ?, status = ?, requests = ?, note = ? WHERE run_id = ?",
                        [store.now_utc(), status, summary["requests"], payload, run_id])

    # ---- (2) tickers file (network first, then a short session) -------------------------------------------
    t0 = time.monotonic()
    fetched_at = store.now_utc()
    try:
        resp = fetch(TICKERS_URL)
    except Blocked as e:
        mark_blocked(TICKERS_URL, e)
        summary.update(stopped_reason="blocked", blocked_at=TICKERS_URL)
        finish("blocked", f"tickers: {e.reason}")
        return summary
    except KeyboardInterrupt:
        summary["stopped_reason"] = "interrupted"
        finish("interrupted")
        return summary
    except (OSError, http.client.HTTPException) as e:
        summary["stopped_reason"] = "tickers_error"
        finish("error", _err_note(redact, "tickers: ", e))
        return summary
    if resp.status != 200:
        summary["stopped_reason"] = "tickers_error"
        finish("error", f"tickers: http_{resp.status}")
        return summary
    try:
        tickers = parse_tickers(resp.body)
    except ValueError as e:
        summary["stopped_reason"] = "tickers_error"
        finish("error", _err_note(redact, "tickers: bad_json: ", e))
        return summary
    summary["tickers_rows"] = len(tickers)
    report: dict = {}
    try:
        raw_path, digest = store.save_raw(cfg, TICKERS_SOURCE, "company_tickers_exchange", resp.body)
        with store.session(cfg, wait_s=FINAL_WAIT_S) as con:
            tick_snap = store.record_snapshot(
                con, source_id=TICKERS_SOURCE, kind="company_tickers_exchange", request=_req_record(TICKERS_URL),
                raw_path=raw_path, raw_sha256=digest, raw_bytes=len(resp.body), rows=len(tickers),
                duration_s=round(time.monotonic() - t0, 3), fetched_at=fetched_at)
            summary["snapshots"] += 1
            lines = _load_lines(con, only_universe)
            mapping = map_securities_to_cik(lines, tickers, report=report)
            store.upsert_many(con, "identifiers", ["security_id", "id_type", "id_value", "method", "snapshot_id"],
                              [[sid, ID_TYPE, str(cik), method, tick_snap] for sid, cik, method in mapping])
            # a considered line that no longer maps (ticker reassigned / dropped from the SEC file) loses its CIK
            mapped = {sid for sid, _, _ in mapping}
            unmapped = sorted({ln["security_id"] for ln in lines} - mapped)
            if unmapped:
                summary["identifiers_removed"] = con.execute(
                    "SELECT count(*) FROM identifiers WHERE id_type = ? AND list_contains(?::VARCHAR[], security_id)",
                    [ID_TYPE, unmapped]).fetchone()[0]
                con.execute("DELETE FROM identifiers WHERE id_type = ? AND list_contains(?::VARCHAR[], security_id)",
                            [ID_TYPE, unmapped])
            stored = {}
            for cik, acc in con.execute(
                    "SELECT cik, accession FROM documents WHERE source_id = ? "
                    "ORDER BY filing_date DESC NULLS LAST, fetched_at DESC", [SOURCE_ID]).fetchall():
                stored.setdefault(str(cik), acc)
            attempts = dict(con.execute("SELECT security_id, attempts FROM crawl_state WHERE source_id = ?",
                                        [SOURCE_ID]).fetchall())
    except KeyboardInterrupt:
        summary["stopped_reason"] = "interrupted"
        finish("interrupted")
        return summary
    summary["us_lines"] = len(lines)
    summary["securities_mapped"] = len(mapping)
    summary["ambiguous"] = report.get("ambiguous", [])
    for _, _, m in mapping:
        summary["mapped_by_method"][m] = summary["mapped_by_method"].get(m, 0) + 1

    # ---- (3) work queue: distinct CIKs, largest market cap first ----------------------------------------
    by_sid = {ln["security_id"]: ln for ln in lines}
    groups: dict[int, list[dict]] = {}
    for sid, cik, _ in mapping:
        groups.setdefault(cik, []).append(by_sid[sid])
    for g in groups.values():
        g.sort(key=lambda ln: (-(ln["market_cap_usd"] or float("-inf")), ln["security_id"]))
    queue = sorted(groups, key=lambda c: (-(groups[c][0]["market_cap_usd"] or float("-inf")), c))
    if wanted is not None:
        queue = [c for c in queue if any(_line_matches(ln, wanted) for ln in groups[c])]
    if limit is not None:
        queue = queue[:max(0, int(limit))]
    summary["ciks_queued"] = len(queue)

    batch = _Batch()
    consecutive_errors = 0
    locked_flushes = 0
    next_flush_at = batch_size          # pending CIKs at which the next mid-run flush is attempted

    def event(cik: int, status: str, note: str | None) -> None:
        if on_company is not None:
            try:
                on_company([ln["security_id"] for ln in groups[cik]], status, redact(note))
            except Exception:  # noqa: BLE001 - a callback never stops the sync
                pass

    def state(cik: int, status: str, http_status: int | None, note: str | None, at: dt.datetime) -> None:
        for ln in groups[cik]:
            sid = ln["security_id"]
            batch.states.append([SOURCE_ID, sid, status, http_status, (attempts.get(sid) or 0) + 1, at, note])
        counts[status] += 1
        event(cik, status, note)
        if status != "ok":
            key = (note or status).split(":", 1)[0].split(";", 1)[0].strip() or status
            summary["failures_by_note"][key] = summary["failures_by_note"].get(key, 0) + 1

    def flush(note: str | None = None, *, wait_s: float = FINAL_WAIT_S) -> None:
        nonlocal batch
        summary["snapshots"] += _flush(cfg, batch, attempts, redact, note, wait_s=wait_s)
        batch = _Batch()

    def flush_mid_run() -> None:
        """Short wait; on another process's lock keep the batch and retry after another batch_size CIKs."""
        nonlocal locked_flushes, next_flush_at
        try:
            flush(wait_s=FLUSH_WAIT_S)
        except store.StoreLocked:
            locked_flushes += 1
            next_flush_at = batch.ciks + batch_size
            log(f"store locked by another process; keeping {batch.ciks} pending CIKs "
                f"(locked flush {locked_flushes}/{MAX_LOCKED_FLUSHES})")
            if locked_flushes >= MAX_LOCKED_FLUSHES or batch.ciks >= MAX_PENDING_CIKS:
                summary["stopped_reason"] = "store_locked"
            return
        locked_flushes = 0
        next_flush_at = batch_size

    def get_json(url: str, kind: str, at: dt.datetime, cik: int, prefix: str) -> Any:
        """GET + parse + save raw + manifest entry. Returns the JSON, or None after recording an error state."""
        t = time.monotonic()
        try:
            resp = fetch(url)
        finally:
            batch.duration_s += time.monotonic() - t
        if resp.status != 200:
            state(cik, "error", resp.status, f"{prefix}_http_{resp.status}", at)
            return None
        try:
            data = json.loads(resp.body)
        except ValueError:
            state(cik, "error", resp.status, f"{prefix}_bad_json", at)
            return None
        path, dig = store.save_raw(cfg, TICKERS_SOURCE, kind, resp.body)
        batch.subs.append({"cik": cik, "url": url, "raw_path": path, "raw_sha256": dig, "raw_bytes": len(resp.body),
                           "fetched_at": at.isoformat()})
        return data

    def find_filing(cik: int, subs: Mapping[str, Any], at: dt.datetime) -> tuple[dict | None, str | None, bool]:
        """Latest annual filing from `recent`, else from up to MAX_OLDER_PAGES older pages (heavy filers such as
        large banks fill `recent` with 424B2/FWP). Returns (filing, failure note, error_recorded)."""
        notes: list[str] = []
        filing = latest_annual_filing(subs, forms, notes=notes)
        files = [f for f in ((subs.get("filings") or {}).get("files") or []) if isinstance(f, Mapping)]
        if not files or (filing is not None and not filing["amendment"]):
            return filing, (notes[0] if notes else None), False
        searched = 0
        for entry in files[:MAX_OLDER_PAGES]:
            name = str(entry.get("name") or "")
            if not _PAGE_NAME_RE.fullmatch(name):
                continue
            searched += 1
            summary["older_pages"] += 1
            page = get_json(SUBMISSIONS_PAGE_URL.format(name=name), f"submissions-{name[:-5]}", at, cik,
                            "submissions_page")
            if page is None:
                return None, None, True
            older = latest_annual_filing({"filings": {"recent": page}}, forms)
            if older is not None and (filing is None or not older["amendment"]):
                filing = dict(older, flags=older["flags"] + [f"older_page:{searched}"])
                if not older["amendment"]:
                    break
        if filing is not None:
            return filing, None, False
        exhausted = len(files) <= MAX_OLDER_PAGES
        return None, ("no_annual_filing" if exhausted else f"no_annual_filing_in_recent_and_{searched}_older_pages"), \
            False

    def process(cik: int) -> str:
        """Network + parsing for one CIK (no DB). Returns the crawl status recorded."""
        at = store.now_utc()
        batch.started_at = batch.started_at or at
        subs = get_json(SUBMISSIONS_URL.format(cik=cik), f"submissions-CIK{cik:010d}", at, cik, "submissions")
        if subs is None:
            return "error"
        filing, fnote, errored = find_filing(cik, subs, at)
        if errored:
            return "error"
        if filing is None:
            state(cik, "no_annual_filing", 200, fnote or "no_annual_filing", at)
            return "no_annual_filing"
        base = filing["base_form"]
        section = section_for_form(base)
        if section is None:
            state(cik, "form_not_supported", 200,
                  "form_40F_aif_in_exhibit_not_supported" if base == "40-F" else f"form_not_supported:{base}", at)
            summary["not_supported"] += 1
            return "form_not_supported"
        if not refresh and stored.get(str(cik)) == filing["accession"]:
            summary["skipped_unchanged"] += 1
            event(cik, "skipped_unchanged", None)
            return "skipped"
        doc_url = archive_url(cik, filing["accession"], filing["primary_document"])
        t = time.monotonic()
        try:
            dresp = fetch(doc_url, max_bytes=MAX_DOC_BYTES, deadline_s=DOC_DEADLINE_S)
        finally:
            batch.duration_s += time.monotonic() - t
        if dresp.status != 200:
            state(cik, "error", dresp.status, f"document_http_{dresp.status}", at)
            return "error"
        raw = dresp.body or b""
        backend = html_backend()
        try:
            text = html_to_text(raw)
            sect, xnote = extract_section(text, base)
        except Exception as e:  # parser failure on a malformed document must not stop the run
            sect, xnote = None, f"html_parse_failed:{type(e).__name__}"
        if getattr(dresp, "truncated", False):
            xnote += ";truncated"
        if filing["amendment"]:
            xnote += ";amendment"
        for flag in filing.get("flags") or ():
            xnote += f";{flag}"
        primary = groups[cik][0]
        row = {"doc_id": f"{SOURCE_ID}:{cik}:{filing['accession']}:{section}", "security_id": primary["security_id"],
               "company_key": primary["company_key"], "source_id": SOURCE_ID, "cik": str(cik), "form": filing["form"],
               "section": section, "accession": filing["accession"], "filing_date": _date(filing["filing_date"]),
               "report_date": _date(filing["report_date"]), "url": doc_url, "raw_sha256": store.sha256(raw),
               "raw_bytes": len(raw), "text_path": None, "text_sha256": None, "text_chars": None,
               "extractor": f"{EXTRACTOR_VERSION}/{backend}", "extract_note": xnote, "fetched_at": at}
        if sect is None:
            batch.docs.append(row)
            state(cik, "extract_failed", 200, xnote, at)
            return "extract_failed"
        tp = text_path_for(cfg, cik, filing["accession"], section)
        tp.parent.mkdir(parents=True, exist_ok=True)
        data = sect.encode("utf-8")
        tp.write_bytes(data)
        row.update(text_path=str(tp), text_sha256=store.sha256(data), text_chars=len(sect))
        batch.docs.append(row)
        desc = short_description(description_source(sect, base))
        if desc:
            for ln in groups[cik]:
                batch.descs.append({"security_id": ln["security_id"], "source_id": SOURCE_ID,
                                    "company_key": ln["company_key"], "text": desc,
                                    "text_sha256": store.sha256(desc.encode("utf-8")), "lang": "en",
                                    "source_url": doc_url, "fetched_at": at, "match_method": "sec_cik",
                                    "match_score": 1.0})
        state(cik, "ok", 200, xnote, at)
        summary["ok_by_form"][base] = summary["ok_by_form"].get(base, 0) + 1
        return "ok"

    try:
        for cik in queue:
            summary["ciks_attempted"] += 1
            try:
                status = process(cik)
            except Blocked as e:
                mark_blocked(SUBMISSIONS_URL.format(cik=cik), e)
                state(cik, "blocked", e.status, f"blocked: {e.reason}", store.now_utc())
                summary.update(stopped_reason="blocked", blocked_at=cik)
                break
            except (OSError, http.client.HTTPException) as e:  # URLError is an OSError
                state(cik, "error", None, _err_note(redact, "network: ", e), store.now_utc())
                status = "error"
            consecutive_errors = consecutive_errors + 1 if status == "error" else 0
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                summary["stopped_reason"] = "consecutive_errors"
                break
            batch.ciks += 1
            if batch.ciks >= next_flush_at:
                flush_mid_run()
                if summary["stopped_reason"]:
                    break
            if progress_every and summary["ciks_attempted"] % progress_every == 0:
                log(f"{summary['ciks_attempted']}/{len(queue)} {counts}")
    except KeyboardInterrupt:
        summary["stopped_reason"] = "interrupted"
    except BaseException:
        summary["stopped_reason"] = "crashed"
        try:
            flush(note="run stopped: crashed")
        finally:
            finish("error", "crashed")
        raise
    run_status = {"blocked": "blocked", "consecutive_errors": "stopped_errors", "interrupted": "interrupted",
                  "store_locked": "store_locked"}.get(summary["stopped_reason"], "ok")
    summary.update({f"status_{k}": v for k, v in counts.items()})
    try:
        flush(note=f"run stopped: {summary['stopped_reason']}" if summary["stopped_reason"] else None)
    finally:
        finish(run_status)     # attempted even when the final flush fails
    return summary
