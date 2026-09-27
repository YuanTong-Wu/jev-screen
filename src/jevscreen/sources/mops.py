"""MOPS / TWSE adapter: Taiwan (TWSE / TPEx) lines -> 股東會年報 (annual report) -> 營運概況 / 業務內容 (L2 evidence),
and optionally the MOPS basic-data field 主要經營業務 (a short official description).

Sources (provenance.py):
- 'mops_annual_report' (official-private): the Chinese annual report PDF (file type F04) from TWSE's e-document
  server doc.twse.com.tw, the server MOPS itself links to (mops.twse.com.tw 年報及股東會相關資料 posts to
  doc.twse.com.tw/server-java/t57sb01). The section text is kept in local files under <home>/docs/mops/<code>/ and
  never shipped; only metadata goes into `documents`.
- 'mops_basic' (official-private): mops.twse.com.tw/mops/api/t05st03 (the JSON API behind MOPS 公司基本資料),
  field mainBusiness (主要經營業務, ~60-300 chars) -> `descriptions` (mode 'basic').
- Not used: TWSE OpenAPI t187ap03_L (official-open, 公司基本資料) has no business text (measured 2026-09-27: 33
  fields, 1,095 TWSE companies, 1.3 MB) and the TPEx one likewise; they add nothing to the symbol -> code mapping.

Measured live 2026-09-27 (research probe, 19 requests; details in docs/DATA_RULES.md):
- robots.txt: doc.twse.com.tw and mopsov.twse.com.tw answer 'User-agent: * / Disallow: /' (mopsov allows only
  bingbot on /mops/web); mops.twse.com.tw and openapi.twse.com.tw have no robots.txt (404). No block, captcha or
  challenge was seen at 1 request per 1.2 s.
- Per company, mode 'annual': 3 requests on doc.twse.com.tw: (1) GET the listing of one meeting year
  (t57sb01?step=1&co_id=<code>&year=<ROC year>&mtype=F: Big5 HTML, ~8 KB, 0.6-1.5 s); (2) POST step=9 with the
  file name (a 503-byte Big5 page whose link /pdf/<name>_<YYYYMMDD_HHMMSS>.pdf is a temporary copy made for this
  request, 0.6-0.8 s); (3) GET that PDF (2.4 MB in 4.9 s, 7.0 MB in 9.2 s: ~0.5-0.8 MB/s). A fourth request when the
  current meeting year has no annual report yet (January-May) and the previous year is listed.
- Mode 'basic': 1 POST per company (JSON, ~5 KB, ~0.5 s).

Flow of mode 'annual' (fetch_annual_report(), usable alone for on-demand L2 text; sync() adds the DB side):
1. TradingView lines on TWSE / TPEX whose symbol is a 4-digit company code ([1-9]ddd; KY companies included) map to
   the MOPS company code (identifiers id_type 'mops_co_id', method 'symbol'). REITs ('01001T'), ETFs and other
   non-company codes are not queued (summary non_company_code).
2. Listing of the current ROC meeting year (today - 1911), else of the previous one. Rows: 證券代號, 資料年度 (the
   fiscal year, ROC), 資料類型, ..., 資料細節說明, 備註, 電子檔案 (file name), 檔案大小, 上傳日期. The annual report is
   the row whose file is '<AD fiscal year>_<code>_<meeting date>F04.pdf' and whose description names 年報 (the
   English copy is FE4, not used): newest fiscal year, then newest upload. A verified listing page has the title
   '電子資料查詢作業'; '查無所需資料' is an empty year. accession = the file name (skip test: same file + extractor;
   a stored copy of the fiscal year before today's is the newest that can exist and is skipped with no request,
   unless it is an extract_failed row without text: that one is listed again).
3. POST step=9 -> the temporary /pdf/ link -> GET the PDF (deadline PDF_DEADLINE_S, cap MAX_PDF_BYTES).
4. Text: PyMuPDF (pypdf fallback), running headers / page numbers dropped, lines re-flowed into paragraphs (helpers
   shared with sources/cninfo.py). Section = from the heading line '業務內容' ('一、業務內容', '5.1 業務內容',
   '(一)業務內容'; the whole line, so TOC rows with page numbers never match) to the first of: '市場及產銷概況'
   (the next item of the standard layout), the next chapter ('伍、'...), the next item of the start's own numbering
   ('二、' after '一、', '5.2' after '5.1'), or MAX_SECTION_CHARS. The first candidate whose text reaches
   MIN_SECTION_CHARS wins (a TOC entry ends at its own next TOC row and stays short); a candidate followed by >= 4
   TOC rows in page order (dot leaders / a dash, or a numbered heading, then a page number; '晶圓 85' table rows do
   not count) is skipped (note 'toc_skipped:N' when nothing is found). Without one, the longest chapter '營運概況' up
   to the next chapter is used (note 'fallback:chapter', CHAPTER_MAX_CHARS). No text layer (a scanned
   report) -> 'extract_failed' note 'no_text_layer'. Kangxi / CJK-radical code points some PDFs use for ideographs
   are mapped back ('⼿' -> '手'). Short description: first real paragraphs, business-registration items skipped.
5. Refusals: the MOPS security page ('FOR SECURITY REASONS' / '因為安全性考量' / '查詢過於頻繁') and the F5 BIG-IP ASM
   rejection page ('The requested URL was rejected' / 'Your support ID is') stop the run at once like a block (ApiStop
   access_refused, cooldown marker). REFUSAL_STOP (3) consecutive unverified answers (a listing
   that is neither rows nor '查無所需資料', a step-9 page without link, a 'PDF' that is not one, a 3xx) stop the run
   the same way. http.Blocked (403 / 429 / challenge) stops it at once. 5 consecutive errors stop it. Mode 'basic':
   a refusal / throttle message in the JSON envelope stops at once; any other code != 200 counts as unverified.

Smoke test 2026-09-27 (temporary home; 2330, 2881, 4958 KY, 5274 TPEx, 4438): 5/5 ok, 15 requests, 23.7 MB in
110 s (~22 s per company); basic mode 5 requests in 9 s.

UNSEEN LIVE (validate on the first larger run): MOPS's refusal page on doc.twse.com.tw / the JSON API (the markers
are the ones of the classic MOPS site); reports without any '業務內容' heading; fiscal years not ending in December
(report_date assumes 12-31).
"""
from __future__ import annotations

import contextlib
import datetime as dt
import html as htmlmod
import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote, urlencode

from .. import guard, store
from ..config import Config
from ..http import Blocked
from .edinet import (FINAL_WAIT_S, ApiStop, SyncRun, decode_bytes, filter_lines, load_lines,
                     prepare_client, short_description_cjk, sigterm_as_interrupt, split_sid, stale_flag,
                     stored_documents)

SOURCE_ID = "mops_annual_report"
BASIC_SOURCE_ID = "mops_basic"
DOC_ORIGIN = "https://doc.twse.com.tw"
LIST_URL = DOC_ORIGIN + "/server-java/t57sb01?step=1&colorchg=1&co_id={code}&year={year}&mtype=F&"
FILE_URL = DOC_ORIGIN + "/server-java/t57sb01"          # POST step=9 -> page with the temporary /pdf/ link
BASIC_API_URL = "https://mops.twse.com.tw/mops/api/t05st03"
BASIC_PAGE_URL = "https://mops.twse.com.tw/mops/#/web/t05st03?companyId={code}"   # human-facing page (recorded)
COMMAND = "sync-mops"
BUDGET = "mops"                          # one process at a time for doc.twse.com.tw + mops.twse.com.tw
DOC_RATE_KEY = "doc.twse.com.tw"
API_RATE_KEY = "mops.twse.com.tw"
DOC_MIN_INTERVAL_S = 1.5                 # >= 1.5 s between request starts (robots.txt disallows crawlers: stay slow)
API_MIN_INTERVAL_S = 2.0                 # the classic MOPS site refused bursts; the JSON API is not measured yet
ID_TYPE = "mops_co_id"
EXTRACTOR_VERSION = "mops-ar-v2"          # v2: TOC rows need leaders or numbering in page order (v1 skipped tables)
PARTIAL_SUFFIX = "/partial"
FORM_LABEL = "股東會年報"
SECTION = "business"
MODES = ("annual", "basic")
TW_VENUES = frozenset({"TWSE", "TPEX"})
TPE = dt.timezone(dt.timedelta(hours=8))
ROC_OFFSET = 1911

MIN_SECTION_CHARS = 200
MAX_SECTION_CHARS = 60_000
CHAPTER_MAX_CHARS = 30_000
MAX_BASIC_CHARS = 2_000
MAX_PAGE_BYTES = 4 * 1024 * 1024
MAX_PDF_BYTES = 128 * 1024 * 1024
LIST_DEADLINE_S = 60.0
FILE_DEADLINE_S = 60.0
PDF_DEADLINE_S = 300.0
API_DEADLINE_S = 60.0
REFUSAL_STOP = 3
COOLDOWN_HOURS = 24                      # fetch_company() refuses within this long of a recorded block

CRAWL_STATUSES = ("ok", "no_annual_filing", "no_basic", "extract_failed", "error", "blocked")

PAGE_TITLE = "電子資料查詢作業"
NO_DATA_MARK = "查無所需資料"
# The classic MOPS refusal page ('FOR SECURITY REASONS, THIS PAGE CAN NOT BE ACCESSED!') and its Chinese variants,
# then the F5 BIG-IP ASM rejection page TWSE hosts sit behind (HTTP 200, title 'Request Rejected', 'The requested URL
# was rejected. Please consult with your administrator. Your support ID is: ...'): known refusals stop the run at once.
REFUSAL_MARKERS = ("FOR SECURITY REASONS", "THIS PAGE CAN NOT BE ACCESSED", "因為安全性考量", "查詢過於頻繁",
                   "因為安全性考量，您所執行的頁面無法呈現",
                   "The requested URL was rejected", "Your support ID is", "<title>Request Rejected")


class MopsRefused(ApiStop):
    """MOPS / TWSE answered with its security (anti-scraping) page: stop the run, cooldown marker."""

    def __init__(self, where: str, message: str | None = None):
        super().__init__("access_refused", where, message=message, cooldown=True)


# ============================================================================================ small helpers


def roc_year(d: dt.date) -> int:
    return d.year - ROC_OFFSET


def today_tpe() -> dt.date:
    return dt.datetime.now(TPE).date()


def _cell(fragment: str) -> str:
    s = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", htmlmod.unescape(s).replace("\xa0", " ")).strip()


def refusal_marker(text: str) -> str | None:
    """The refusal marker found in the first 64 KB of a page, else None."""
    head = text[:65536]
    up = head.upper()
    return next((m for m in REFUSAL_MARKERS if (m.upper() in up if m.isascii() else m in head)), None)


def page_note(body: bytes, text: str) -> str:
    """';bytes:<n>;sha:<12 hex>;title:<page title>' of a page that is not what was asked for."""
    m = re.search(r"<title\b[^>]*>(.*?)</title\s*>", text, re.S | re.I)
    title = _cell(m.group(1))[:60] if m else ""
    return f";bytes:{len(body)};sha:{store.sha256(body)[:12]}" + (f";title:{title}" if title else "")


def _roc_datetime(s: str) -> dt.datetime | None:
    """'115/05/21 19:02:58' (ROC) -> datetime(2026, 5, 21, 19, 2, 58); '115/05/21' -> midnight; else None."""
    m = re.match(r"^\s*(\d{2,3})/(\d{1,2})/(\d{1,2})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?", s or "")
    if not m:
        return None
    try:
        return dt.datetime(int(m.group(1)) + ROC_OFFSET, int(m.group(2)), int(m.group(3)), int(m.group(4) or 0),
                           int(m.group(5) or 0), int(m.group(6) or 0))
    except ValueError:
        return None


# ============================================================================================ mapping


def map_securities_to_mops(securities: Sequence[Mapping[str, Any]], *,
                           report: dict | None = None) -> list[tuple[str, str, str]]:
    """TradingView lines on TWSE / TPEX -> MOPS company code (the 4-digit symbol itself).
    Returns [(security_id, code, 'symbol')]; `report` receives considered, non_tw, unmatched (non-company codes:
    REITs '01001T', ETFs '0050', 6-digit codes), non_company_code (the first 20 of them) and ambiguous (always [])."""
    rep: dict[str, Any] = {"considered": 0, "non_tw": 0, "unmatched": 0, "non_company_code": [], "ambiguous": []}
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for sec in securities:
        sid = str(sec.get("security_id") or "")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        exch, sym = split_sid(sec)
        if exch not in TW_VENUES or not sym:
            rep["non_tw"] += 1
            continue
        rep["considered"] += 1
        sym = unicodedata.normalize("NFKC", sym).strip().upper()
        if not re.fullmatch(r"[1-9]\d{3}", sym):
            rep["unmatched"] += 1
            if len(rep["non_company_code"]) < 20:
                rep["non_company_code"].append(sid)
            continue
        out.append((sid, sym, "symbol"))
    if report is not None:
        report.update(rep)
    return out


# ============================================================================================ listing


def parse_listing(text: str) -> tuple[list[dict], str]:
    """t57sb01 step=1 page -> (rows, state). state: 'rows' | 'no_data' ('查無所需資料') | 'unverified' (neither: an
    error / throttling page). Row keys: code, data_year (ROC int or None), kind, meeting, desc, note, filename, size,
    uploaded (datetime or None)."""
    rows: list[dict] = []
    for tr in re.findall(r"<tr\b[^>]*>(.*?)</tr\s*>", text, re.S | re.I):
        tds = re.findall(r"<td\b[^>]*>(.*?)</td\s*>", tr, re.S | re.I)
        if len(tds) < 10:
            continue
        cells = [_cell(td) for td in tds]
        link = re.search(r"""readfile2?\(\s*["']\w+["']\s*,\s*["']([^"']*)["']\s*,\s*["']([^"']+)["']\s*\)""", tr)
        filename = (link.group(2) if link else cells[7]).strip()
        if not re.search(r"\.\w{2,4}$", filename):
            continue
        year = re.match(r"\s*(\d{2,3})", cells[1])
        size = re.sub(r"[^\d]", "", cells[8])
        rows.append({"code": cells[0], "data_year": int(year.group(1)) if year else None, "kind": cells[2],
                     "meeting": cells[4], "desc": cells[5], "note": cells[6], "filename": filename,
                     "size": int(size) if size else None, "uploaded": _roc_datetime(cells[9])})
    if rows:
        return rows, "rows"
    if NO_DATA_MARK in text:
        return [], "no_data"
    return [], "unverified"


_F04_RE = re.compile(r"^(\d{4})_([0-9A-Za-z]+)_(\d{8})F04\.pdf$", re.I)


def pick_annual_report(rows: Iterable[Mapping[str, Any]], code: str | None = None) -> dict | None:
    """The Chinese annual report (股東會年報, file '<AD year>_<code>_<meeting date>F04.pdf') among listing rows:
    newest fiscal year, then newest upload, then file name. Returns the row plus meeting_date (date) or None."""
    cands = []
    for r in rows:
        m = _F04_RE.match(str(r.get("filename") or ""))
        if not m or "年報" not in str(r.get("desc") or "") or "英文" in str(r.get("desc") or ""):
            continue
        if code and m.group(2).upper() != str(code).upper():
            continue
        try:
            meeting = dt.datetime.strptime(m.group(3), "%Y%m%d").date()
        except ValueError:
            meeting = None
        fy = r.get("data_year") if r.get("data_year") is not None else int(m.group(1)) - ROC_OFFSET
        cands.append(dict(r, data_year=fy, meeting_date=meeting))
    if not cands:
        return None
    return max(cands, key=lambda c: (c["data_year"] or 0, c.get("uploaded") or dt.datetime.min, c["filename"]))


def parse_file_page(text: str) -> str | None:
    """step=9 page -> the temporary '/pdf/<name>.pdf' path, else None."""
    m = re.search(r"""href\s*=\s*["']?(/pdf/[^"'\s>]+?\.pdf)["'\s>]""", text, re.I)
    return m.group(1) if m else None


# ============================================================================================ PDF -> section


def _compact(line: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", line))


_PREFIX = (r"(?:(?P<cn>[一二三四五六七八九十]{1,3})[、.．]"
           r"|(?P<dot>\d{1,2}(?:\.\d{1,2}){0,2})[.、．]?"
           r"|[(（](?P<pn>[一二三四五六七八九十]{1,3})[)）]"
           r"|(?P<chap>[壹貳參叁肆伍陸柒捌玖拾]{1,3})[、.．])?")
_START_RE = re.compile(rf"^{_PREFIX}業務內容[:：]?$")
_MARKET_RE = re.compile(rf"^{_PREFIX}市場(?:及|與)產銷概況")
_CHAPTER_RE = re.compile(r"^(?:[壹貳參叁肆伍陸柒捌玖拾]{1,3}[、.．]|第[一二三四五六七八九十]{1,3}章)")
_OPS_CHAPTER_RE = re.compile(r"^(?:[壹貳參叁肆伍陸柒捌玖拾]{1,3}[、.．]|第[一二三四五六七八九十]{1,3}章|\d{1,2}[.、．]?)?"
                             r"營運概況[:：]?$")
_SENTENCE_RE = re.compile(r"[。；！？]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# a table-of-contents row: a short title with CJK text, then dot leaders / an ellipsis / a dash and a page number
# ('業務範圍 ........ 45', '5.1 業務內容 ‑ 98'), or a numbered heading, spaces and a page number ('(一)業務範圍 45').
# A bare 'text number' row without numbering is a table row ('晶圓 85'), not a TOC row.
_TOC_LEADER_RE = re.compile(r"^(?=.{0,40}[\u4e00-\u9fff]).{2,50}?(?:\.{3,}|(?:…\s*){2,}|·{3,}|\s*[‑\-–—])\s*"
                            r"(\d{1,3})$")
_TOC_NUMBERED_RE = re.compile(r"^(?:[一二三四五六七八九十]{1,3}[、.．]|[(（](?:[一二三四五六七八九十]{1,3}|\d{1,2})[)）]"
                              r"|\d{1,2}(?:\.\d{1,2}){0,2}[.、．]?\s*(?=[\u4e00-\u9fff])|[壹貳參叁肆伍陸柒捌玖拾]{1,3}[、.．]"
                              r"|第[一二三四五六七八九十]{1,3}章)"
                              r"(?=.{0,40}[\u4e00-\u9fff]).{1,48}?\s+(\d{1,3})$")
_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_number(s: str) -> int:
    if s in _CN_DIGITS:
        return _CN_DIGITS[s]
    if s.startswith("十"):
        return 10 + _CN_DIGITS.get(s[1:], 0)
    if len(s) >= 2 and s[1] == "十":
        return _CN_DIGITS.get(s[0], 0) * 10 + _CN_DIGITS.get(s[2:], 0)
    return 0


def _is_heading(c: str, max_chars: int = 40) -> bool:
    return 0 < len(c) <= max_chars and not _SENTENCE_RE.search(c)


def _toc_page(line: str) -> int | None:
    """The page number of a table-of-contents row, else None."""
    ln = line.strip()
    m = _TOC_LEADER_RE.search(ln) or _TOC_NUMBERED_RE.search(ln)
    return int(m.group(1)) if m else None


def _toc_like(lines: Sequence[str]) -> bool:
    """True when at least 4 of these lines are table-of-contents rows (_toc_page) whose page numbers never go down
    (a TOC is in page order; a share table such as '(一)晶圓 85' / '(二)光罩 5' is not)."""
    pages = [pg for pg in map(_toc_page, lines) if pg is not None]
    return len(pages) >= 4 and all(a <= b for a, b in zip(pages, pages[1:]))


def _end_of(lines: Sequence[str], start: int, m: re.Match, limit: int) -> tuple[int, str]:
    """(end index, reason) of the section whose heading line lines[start] matched _START_RE as `m`."""
    cn = m.group("cn")
    dot = m.group("dot")
    dot_parts = [int(x) for x in dot.split(".")] if dot and "." in dot else None
    chars = 0
    for i in range(start + 1, len(lines)):
        c = _compact(lines[i])
        if _is_heading(c):
            if _MARKET_RE.match(c):
                return i, "market"
            if _CHAPTER_RE.match(c):
                return i, "next_chapter"
            if cn:
                nm = re.match(r"^([一二三四五六七八九十]{1,3})[、.．]\S", c)
                if nm and _cn_number(nm.group(1)) > _cn_number(cn):
                    return i, "next_item"
            if dot_parts:
                depth = len(dot_parts)
                nm = re.match(r"^(\d{1,2}(?:\.\d{1,2}){%d})(?!\d)(?!\.\d)[.、．]?(?=[\u4e00-\u9fff])" % (depth - 1), c)
                if nm:
                    parts = [int(x) for x in nm.group(1).split(".")]
                    if parts[:-1] == dot_parts[:-1] and parts[-1] > dot_parts[-1]:
                        return i, "next_item"
        chars += len(lines[i])
        if chars > limit * 1.5:          # raw lines are a little longer than the re-flowed text
            return i, "cap"
    return len(lines), "end_of_document"


# Kangxi radicals / CJK radicals supplement: some report PDFs map ideographs to them ('⼀' U+2F00 for '一', '⼿' for
# '手', '⻑' U+2ED1 for '長'; seen live 2026-09-27 in a KY company's report), which breaks keyword matching. NFKC maps
# the Kangxi block back.
# CJK Radicals Supplement forms have no NFKC mapping: the unambiguous ones are mapped by hand ('⻑期' seen live).
_RADICAL_RE = re.compile(r"[\u2e80-\u2fdf]")
_RADICAL_SUPPLEMENT = {"\u2e9f": "母", "\u2ea0": "民", "\u2ea9": "王", "\u2ec4": "西", "\u2eca": "足", "\u2ed1": "長",
                       "\u2ed8": "青", "\u2edd": "食", "\u2ee4": "鬼"}


def _unradical(ch: str) -> str:
    return _RADICAL_SUPPLEMENT.get(ch) or unicodedata.normalize("NFKC", ch)


_BARE_MARKER_RE = re.compile(r"^(?:\d{1,2}|[一二三四五六七八九十]{1,3}|[(（](?:\d{1,2}|[一二三四五六七八九十]{1,3}|[a-zA-Z])[)）])"
                             r"[.、．]?$")


def _reflow(lines: Sequence[str]) -> str:
    """Lines -> paragraphs (cninfo.join_paragraphs); control characters dropped (bullets extract as '\\x01'), and a
    list marker alone on its line ('1.', '(一)') joined to the line after it."""
    from .cninfo import join_paragraphs
    clean = [c for c in (_CTRL_RE.sub("", ln).strip() for ln in lines) if c]
    merged: list[str] = []
    for ln in clean:
        if merged and _BARE_MARKER_RE.match(_compact(merged[-1])):
            merged[-1] = merged[-1] + ln
        else:
            merged.append(ln)
    text = _RADICAL_RE.sub(lambda m: _unradical(m.group()), join_paragraphs(merged))
    return "\n".join(ln.strip() for ln in text.split("\n") if ln.strip())


def extract_business_section(lines: Sequence[str]) -> tuple[str | None, str]:
    """Cleaned annual-report lines -> ('業務內容' section text or None, note). See the module doc, step 4."""
    starts = [(i, m) for i, ln in enumerate(lines) for m in [_START_RE.match(_compact(ln))] if m]
    valid: list[tuple[str, str]] = []
    longest_short: int | None = None
    toc_skipped = 0
    for i, m in starts:
        if _toc_like(lines[i + 1:i + 11]):
            toc_skipped += 1
            continue
        end, why = _end_of(lines, i, m, MAX_SECTION_CHARS)
        text = _reflow(lines[i + 1:end])
        if len(text) < MIN_SECTION_CHARS:
            longest_short = len(text) if longest_short is None else max(longest_short, len(text))
            continue
        head = _compact(lines[i])
        note = f"start:{head[:12]};end:{why}"
        if len(text) > MAX_SECTION_CHARS:
            text, note = text[:MAX_SECTION_CHARS], note + ";capped"
        valid.append((text, note))
    if valid:
        text, note = valid[0]
        if len(valid) > 1:
            note += f";candidates:{len(valid)}"
        return text, note
    # fallback: the whole 營運概況 chapter; of several headings (a TOC entry, the real chapter) the longest wins, so
    # no TOC test is needed here (a TOC entry ends at the next chapter's TOC row and stays short)
    best: str | None = None
    for i, ln in enumerate(lines):
        if not _OPS_CHAPTER_RE.match(_compact(ln)):
            continue
        end = len(lines)
        for j in range(i + 1, len(lines)):
            cj = _compact(lines[j])
            if _is_heading(cj) and _CHAPTER_RE.match(cj):
                end = j
                break
        text = _reflow(lines[i + 1:end])
        if len(text) >= MIN_SECTION_CHARS and (best is None or len(text) > len(best)):
            best = text
    if best is not None:
        note = "fallback:chapter"
        if len(best) > CHAPTER_MAX_CHARS:
            best, note = best[:CHAPTER_MAX_CHARS], note + ";capped"
        return best, note
    toc = f";toc_skipped:{toc_skipped}" if toc_skipped else ""
    if starts:
        return None, f"section_too_short:{longest_short or 0};starts:{len(starts)}{toc}"
    return None, "business_section_not_found"


# a business-registration item ('(1)C306010成衣業。', 'H801011金融控股公司業'): the legal scope list, not a description
_REG_CODE_RE = re.compile(r"^(?:[(（]?\d{1,2}[)）.、．]?\s*)?[A-Z]{1,2}\d{5,6}")


def description_from_section(text: str | None, max_chars: int = 800) -> str:
    """Short description from a 業務內容 section: short_description_cjk() over the paragraphs that are not
    business-registration items, paragraphs under 30 characters skipped (list items, captions)."""
    if not text:
        return ""
    paras = [p for p in text.split("\n") if not _REG_CODE_RE.match(_compact(p))]
    return short_description_cjk("\n".join(paras), max_chars=max_chars, min_para=30)


def pdf_to_section(data: bytes) -> tuple[str | None, str]:
    """Annual report PDF bytes -> (section text or None, note incl. backend and page count)."""
    from .cninfo import PdfError, clean_pages, pdf_pages
    try:
        pages, backend = pdf_pages(data)
    except PdfError as e:
        return None, f"pdf_unreadable:{str(e)[:80]}"
    if not any(p.strip() for p in pages):
        return None, f"no_text_layer;pages:{len(pages)};backend:{backend}"
    text, note = extract_business_section(clean_pages(pages))
    return text, f"{note};pages:{len(pages)};backend:{backend}"


# ============================================================================================ single company


def _doc_get(client: Any, url: str, *, deadline_s: float, max_bytes: int):
    return client.get(url, rate_key=DOC_RATE_KEY, deadline_s=deadline_s, max_bytes=max_bytes)


def _location(resp: Any) -> str:
    loc = next((str(v) for k, v in (getattr(resp, "headers", None) or {}).items() if k.lower() == "location"), "")
    return f";location:{loc[:80]}" if loc else ""


def fetch_annual_report(client: Any, code: str, *, as_of: dt.date | None = None,
                        skip: Callable[[dict], bool] | None = None,
                        on_request: Callable[[str], None] | None = None) -> dict:
    """One company's latest annual report business section. No database access; usable on demand.

    Network: listing (current ROC meeting year, then the previous one), step-9 POST, PDF. Returns
    {'status': 'ok' | 'no_annual_filing' | 'extract_failed' | 'error' | 'skipped', 'http_status', 'note', 'filing'
    (listing row + listing_year, list_url, filing_date, report_date, flags) or None, 'text', 'description',
    'pdf_sha256', 'pdf_bytes', 'truncated', 'refused' (an answer that is not what was asked for: counted by sync()
    toward REFUSAL_STOP), 'listings' [(url, body)], 'bytes' (all bodies), 'requests'}.
    `skip(filing)` True -> status 'skipped' before the PDF is requested. Raises http.Blocked, http.RequestTimeout,
    OSError / HTTPException (network) and MopsRefused (the MOPS security page)."""
    code = str(code).strip().upper()
    today = as_of or today_tpe()
    out: dict[str, Any] = {"status": "error", "http_status": None, "note": None, "filing": None, "text": None,
                           "description": None, "pdf_sha256": None, "pdf_bytes": None, "truncated": False,
                           "refused": False, "listings": [], "bytes": 0, "requests": 0}

    def fail(status: str, http_status: int | None, note: str, refused: bool = False) -> dict:
        out.update(status=status, http_status=http_status, note=note, refused=refused)
        return out

    def get(url: str, **kw):
        if on_request:
            on_request(url)
        out["requests"] += 1
        resp = _doc_get(client, url, **kw)
        out["bytes"] += len(resp.body or b"")
        return resp

    filing = None
    tried: list[str] = []
    for year in (roc_year(today), roc_year(today) - 1):
        url = LIST_URL.format(code=quote(code, safe=""), year=year)
        resp = get(url, deadline_s=LIST_DEADLINE_S, max_bytes=MAX_PAGE_BYTES)
        if resp.status != 200:
            return fail("error", resp.status, f"list_http_{resp.status}" + _location(resp),
                        refused=300 <= resp.status < 400)
        text, _ = decode_bytes(resp.body or b"", prefer=("cp950", "big5", "utf-8"))
        marker = refusal_marker(text)
        if marker:
            raise MopsRefused("listing", f"refusal page ({marker}) at {url}")
        rows, state = parse_listing(text)
        if state == "unverified" or PAGE_TITLE not in text:
            return fail("error", resp.status, "list_unverified" + page_note(resp.body or b"", text), refused=True)
        out["listings"].append((url, resp.body or b""))
        tried.append(str(year))
        best = pick_annual_report(rows, code)
        if best is not None:
            filing = dict(best, listing_year=year, list_url=url)
            break
    if filing is None:
        return fail("no_annual_filing", 200, "no_annual_report:" + ",".join(tried))

    fy_end = dt.date(filing["data_year"] + ROC_OFFSET, 12, 31) if filing.get("data_year") else None
    flags = [f for f in [stale_flag(fy_end, today)] if f]
    if filing["listing_year"] != roc_year(today):
        flags.append(f"listing_year:{filing['listing_year']}")
    uploaded = filing.get("uploaded")
    filing.update(report_date=fy_end, filing_date=uploaded.date() if uploaded else None, flags=flags)
    out["filing"] = filing
    if skip is not None and skip(filing):
        out.update(status="skipped", http_status=200, note="unchanged")
        return out

    # step 9: a temporary copy of the file and the page linking to it
    body = urlencode({"colorchg": "1", "step": "9", "kind": "F", "co_id": code, "filename": filing["filename"]})
    if on_request:
        on_request(FILE_URL)
    out["requests"] += 1
    resp = client.request("POST", FILE_URL, data=body.encode("ascii"), rate_key=DOC_RATE_KEY,
                          headers={"Content-Type": "application/x-www-form-urlencoded"},
                          deadline_s=FILE_DEADLINE_S, max_bytes=MAX_PAGE_BYTES)
    out["bytes"] += len(resp.body or b"")
    if resp.status != 200:
        return fail("error", resp.status, f"file_http_{resp.status}" + _location(resp),
                    refused=300 <= resp.status < 400)
    text, _ = decode_bytes(resp.body or b"", prefer=("cp950", "big5", "utf-8"))
    marker = refusal_marker(text)
    if marker:
        raise MopsRefused("file", f"refusal page ({marker}) at {FILE_URL}")
    path = parse_file_page(text)
    if path is None:
        if NO_DATA_MARK in text:
            return fail("error", resp.status, "file_not_found" + page_note(resp.body or b"", text))
        return fail("error", resp.status, "file_link_missing" + page_note(resp.body or b"", text), refused=True)

    pdf_url = DOC_ORIGIN + path
    resp = get(pdf_url, deadline_s=PDF_DEADLINE_S, max_bytes=MAX_PDF_BYTES)
    if resp.status != 200:
        return fail("error", resp.status, f"pdf_http_{resp.status}" + _location(resp),
                    refused=300 <= resp.status < 400)
    data = resp.body or b""
    if b"%PDF-" not in data[:1024]:            # PDF readers accept junk before the header; so do we
        head, _ = decode_bytes(data[:65536], prefer=("cp950", "big5", "utf-8"))
        marker = refusal_marker(head)
        if marker:
            raise MopsRefused("pdf", f"refusal page ({marker}) instead of the PDF")
        return fail("error", resp.status, "not_pdf" + page_note(data, head), refused=True)
    out.update(pdf_sha256=store.sha256(data), pdf_bytes=len(data), truncated=bool(getattr(resp, "truncated", False)))
    section, note = pdf_to_section(data)
    note = f"ar;{note};file:{filing['filename']}"
    if out["truncated"]:
        note += ";truncated"
    for f in flags:
        note += f";{f}"
    if section is None:
        return fail("extract_failed", 200, note)
    out.update(status="ok", http_status=200, note=note, text=section, description=description_from_section(section))
    return out


class CooldownActive(RuntimeError):
    """fetch_company() refused to start: a block of sync-mops (or of an earlier fetch_company) was recorded less than
    COOLDOWN_HOURS ago (pass after_block=True to override). No request was made."""


def fetch_company(cfg: Config, code: str, client: Any = None, *, as_of: dt.date | None = None,
                  after_block: bool = False) -> dict:
    """fetch_annual_report() on a polite client of its own (>= DOC_MIN_INTERVAL_S), holding the 'mops' budget lock
    so it never runs beside a sync. For on-demand L2 text: no database access, nothing written except the cooldown
    marker. Same cooldown discipline as sync-mops: within COOLDOWN_HOURS of a recorded block it raises
    CooldownActive before any request (unless after_block); http.Blocked or MopsRefused writes the marker (command
    'sync-mops', so the next sync refuses too) before it propagates."""
    if not after_block:
        hit = guard.recent_block_marker(cfg, COMMAND, hours=COOLDOWN_HOURS)
        if hit:
            raise CooldownActive(f"refusing to fetch within {COOLDOWN_HOURS:g} h of a block ({hit['note']}); "
                                 "pass after_block=True to override")
    client = prepare_client(cfg, client, DOC_MIN_INTERVAL_S)
    last: list[str | None] = [None]
    with guard.budget_lock(cfg, BUDGET, reentrant=True):
        try:
            return fetch_annual_report(client, code, as_of=as_of, on_request=lambda url: last.__setitem__(0, url))
        except Blocked as e:
            guard.mark_blocked(cfg, COMMAND, url=e.url or last[0], status=e.status, reason=e.reason)
            raise
        except MopsRefused as e:
            guard.mark_blocked(cfg, COMMAND, url=last[0], status=e.status, reason=e.reason)
            raise


def parse_basic(payload: Any) -> tuple[dict | None, str]:
    """t05st03 JSON -> ({main_business, company_name, industry, abbreviation}, note) or (None, note)."""
    if not isinstance(payload, Mapping):
        return None, "basic_bad_json"
    if str(payload.get("code")) != "200":
        return None, f"basic_code_{payload.get('code')}"
    result = payload.get("result")
    if not isinstance(result, Mapping):
        return None, "basic_no_result"

    def val(k: str) -> str | None:
        v = result.get(k)
        if isinstance(v, Mapping):
            if v.get("isHidden"):
                return None
            v = v.get("value")
        if v is None or isinstance(v, (bool, int, float)):
            return None
        s = re.sub(r"[ \t\r\f\v　\xa0]+", " ", str(v)).strip()
        return s or None

    main = val("mainBusiness")
    info = {"main_business": main[:MAX_BASIC_CHARS] if main else None, "company_name": val("companyName"),
            "industry": val("industryCategory"), "abbreviation": val("companyEnglishAbbreviation")}
    return (info, "basic") if main else (None, "basic_no_main_business")


def fetch_basic(client: Any, code: str, *, on_request: Callable[[str], None] | None = None) -> dict:
    """MOPS basic data of one company: {'status': 'ok' | 'no_basic' | 'error', 'http_status', 'note', 'info', 'body',
    'refused'}. 'no_basic' only for a code-200 answer without (or with a hidden) mainBusiness; a JSON envelope with
    another code is 'error' with refused=True (counted toward REFUSAL_STOP). Raises http.Blocked, network errors and
    MopsRefused (a refusal page, or a refusal / throttle message in the envelope)."""
    code = str(code).strip().upper()
    if on_request:
        on_request(BASIC_API_URL)
    resp = client.request("POST", BASIC_API_URL, data=json.dumps({"companyId": code}).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, rate_key=API_RATE_KEY,
                          deadline_s=API_DEADLINE_S, max_bytes=MAX_PAGE_BYTES)
    body = resp.body or b""
    out = {"status": "error", "http_status": resp.status, "note": None, "info": None, "body": body, "refused": False}
    if resp.status != 200:
        out.update(note=f"basic_http_{resp.status}" + _location(resp), refused=300 <= resp.status < 400)
        return out
    text, _ = decode_bytes(body, prefer=("utf-8", "cp950"))
    try:
        payload = json.loads(text)
    except ValueError:
        marker = refusal_marker(text)
        if marker:
            raise MopsRefused("basic", f"refusal page ({marker}) at {BASIC_API_URL}")
        out.update(note="basic_not_json" + page_note(body, text), refused=True)
        return out
    ok_envelope = isinstance(payload, Mapping) and str(payload.get("code")) == "200"
    # a refusal / throttle message in the envelope (never in the company's own fields) stops the run at once
    marker = refusal_marker(str(payload.get("message") or "") if ok_envelope else text)
    if marker:
        raise MopsRefused("basic", f"refusal message ({marker}) at {BASIC_API_URL}")
    info, note = parse_basic(payload)
    if not ok_envelope:            # code != 200 / not an object: not what was asked for (sync: REFUSAL_STOP)
        out.update(note=note + (page_note(body, text) if note == "basic_bad_json" else ""), refused=True)
        return out
    out.update(status="ok" if info else "no_basic", note=note, info=info)
    return out


# ============================================================================================ sync


def is_newest_possible(stored: tuple[str, str | None] | None, today: dt.date) -> bool:
    """True when a stored (accession, extractor) is a complete current-extractor copy of the fiscal year before
    `today`'s: no newer annual report can exist yet, so the listing request is skipped (a re-uploaded correction of
    the same year needs --refresh)."""
    if not stored or stored[1] != EXTRACTOR_VERSION:
        return False
    m = _F04_RE.match(str(stored[0] or ""))
    return bool(m) and int(m.group(1)) >= today.year - 1


def stored_without_text(con) -> set[str]:
    """MOPS codes whose newest stored document (stored_documents' order) has no text (extract_failed): such a row is
    never 'the newest that can exist', so the listing is fetched again (the PDF only when file or extractor changed)."""
    out: set[str] = set()
    seen: set[str] = set()
    for doc_id, text_path in con.execute(
            "SELECT doc_id, text_path FROM documents WHERE source_id = ? "
            "ORDER BY filing_date DESC NULLS LAST, fetched_at DESC", [SOURCE_ID]).fetchall():
        parts = str(doc_id).split(":")
        if len(parts) >= 3 and parts[1] not in seen:
            seen.add(parts[1])
            if text_path is None:
                out.add(parts[1])
    return out


def text_path_for(cfg: Config, code: str, filename: str) -> Path:
    return Path(cfg.home) / "docs" / "mops" / code / f"{Path(filename).stem}-{SECTION}.txt"


def _identity(s: Any) -> Any:
    return s


class _MopsRun(SyncRun):
    """SyncRun whose request records carry the host's own rate key and no auth (MOPS needs no key)."""

    def request_record(self, urls: Iterable[str] | str) -> dict:
        lst = [urls] if isinstance(urls, str) else list(urls)
        keys = sorted({API_RATE_KEY if str(u).startswith("https://mops.") else DOC_RATE_KEY for u in lst})
        rec: dict[str, Any] = {"method": "GET/POST", "rate_key": ",".join(keys) or DOC_RATE_KEY, "auth": "none"}
        rec["url" if isinstance(urls, str) else "urls"] = urls if isinstance(urls, str) else lst
        return rec


ANNUAL_OPT_IN_MESSAGE = (
    "mode 'annual' downloads each company's annual report from doc.twse.com.tw, whose robots.txt disallows "
    "crawlers ('User-agent: * / Disallow: /'); a pass over all Taiwan companies is ~953 companies, ~4.5 GB and "
    "2.5-6 h. Name the few companies you need (codes / --codes 2330,6223), or ask for the full yearly pass "
    "explicitly (full_pass=True / --full-pass). The default mode 'basic' uses the MOPS API instead.")


def sync(cfg: Config, client: Any = None, *, limit: int | None = None, codes: Iterable[str] | str | None = None,
         refresh: bool = False, min_mcap_usd: float | None = None, mode: str = "basic", full_pass: bool = False,
         batch_size: int = 10, progress_every: int = 25, only_universe: bool = True,
         as_of: dt.date | None = None,
         on_company: Callable[[list[str], str, str | None], None] | None = None) -> dict:
    """Map TWSE / TPEX universe lines to MOPS codes and pull, per company (market cap desc), the MOPS basic-data
    主要經營業務 (mode 'basic', the default, 1 request) or the latest annual report's 業務內容 section (mode 'annual', 3
    requests on doc.twse.com.tw, whose robots.txt disallows crawlers: only for named `codes`, or for all companies
    with full_pass=True; otherwise ValueError before any request). Incremental: 'annual' skips a company whose
    stored document has the same file and extractor, 'basic' one that already has a 'mops_basic' description
    (unless refresh). Per batch one short DB session. Returns a summary whose 'status' is ok | blocked |
    stopped_errors | interrupted | store_locked | error. on_company(security_ids, status, note): per-company events
    (edinet.SyncRun.on_company)."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
    if mode == "annual" and not codes and not full_pass:
        raise ValueError(ANNUAL_OPT_IN_MESSAGE)
    client = prepare_client(cfg, client, DOC_MIN_INTERVAL_S if mode == "annual" else API_MIN_INTERVAL_S)
    with contextlib.ExitStack() as stack:
        stack.enter_context(guard.budget_lock(cfg, BUDGET, reentrant=True))
        stack.enter_context(sigterm_as_interrupt())
        return _sync(cfg, client, limit=limit, codes=codes, refresh=refresh, min_mcap_usd=min_mcap_usd, mode=mode,
                     batch_size=batch_size, progress_every=progress_every, only_universe=only_universe,
                     as_of=as_of, on_company=on_company)


def _sync(cfg: Config, client: Any, *, limit, codes, refresh, min_mcap_usd, mode, batch_size, progress_every,
          only_universe, as_of, on_company=None) -> dict:
    source_id = SOURCE_ID if mode == "annual" else BASIC_SOURCE_ID
    run = _MopsRun(cfg, source_id=source_id, command=COMMAND, client=client, rate_key=DOC_RATE_KEY,
                   redact=_identity, key_placeholder="", log_tag="mops", statuses=CRAWL_STATUSES,
                   batch_size=batch_size, progress_every=progress_every)
    run.on_company = on_company
    summary = run.summary
    summary.update({"mode": mode, "ok_documents": 0, "ok_descriptions": 0, "non_company_code": [],
                    "skipped_without_request": 0})
    today = as_of or today_tpe()
    run.start()
    report: dict = {}
    try:
        with store.session(cfg, wait_s=FINAL_WAIT_S) as con:
            lines = filter_lines(load_lines(con, TW_VENUES, only_universe=only_universe), codes, min_mcap_usd)
            mapping = map_securities_to_mops(lines, report=report)
            snap = store.record_snapshot(
                con, source_id=source_id, kind="mops_code_map",
                request={"derived_from": "securities.symbol", "venues": sorted(TW_VENUES), "rule": "[1-9]ddd"},
                raw_path=None, raw_sha256=None, raw_bytes=None, rows=len(mapping), duration_s=0.0,
                note="MOPS company code = TradingView symbol")
            summary["snapshots"] += 1
            run.write_mapping(con, lines, mapping, ID_TYPE, snap, report)
            summary["non_company_code"] = list(report.get("non_company_code", []))
            stored = stored_documents(con, SOURCE_ID)
            failed = stored_without_text(con)
            have_basic = {r[0] for r in con.execute(
                "SELECT DISTINCT security_id FROM descriptions WHERE source_id = ?", [BASIC_SOURCE_ID]).fetchall()}
            run.attempts = dict(con.execute("SELECT security_id, attempts FROM crawl_state WHERE source_id = ?",
                                            [source_id]).fetchall())
    except KeyboardInterrupt:
        summary["stopped_reason"] = "interrupted"
        return run.close()

    queue = run.queue_from_mapping(lines, mapping, limit)
    if mode == "basic" and not refresh:
        before = len(queue)
        queue = [c for c in queue if not any(ln["security_id"] in have_basic for ln in run.groups[c])]
        summary["skipped_unchanged"] += before - len(queue)
    current: dict[str, str] = {}
    refusals = [0]

    def seen(code: str) -> Callable[[str], None]:
        return lambda url: current.__setitem__(code, url)

    def refused_or_reset(res: Mapping[str, Any]) -> None:
        if res.get("refused"):
            refusals[0] += 1
            if refusals[0] >= REFUSAL_STOP:
                raise ApiStop("access_refused", "unverified_pages", cooldown=True,
                              message=f"{refusals[0]} consecutive MOPS answers without the requested content")
        else:
            refusals[0] = 0

    def process_annual(code: str) -> str:
        at = store.now_utc()
        if not refresh and code not in failed and is_newest_possible(stored.get(code), today):
            summary["skipped_unchanged"] += 1
            summary["skipped_without_request"] += 1
            return "skipped"
        t = time.monotonic()
        try:
            res = fetch_annual_report(client, code, as_of=today, on_request=seen(code),
                                      skip=lambda f: not refresh and stored.get(code) == (f["filename"],
                                                                                        EXTRACTOR_VERSION))
        finally:
            run.batch.duration_s += time.monotonic() - t
        summary["bytes_downloaded"] += res["bytes"]
        for url, body in res["listings"]:
            run.add_aux(f"list-{code}", url, body, at, co_id=code)
        refused_or_reset(res)
        status = res["status"]
        if status == "skipped":
            summary["skipped_unchanged"] += 1
            return "skipped"
        if status in ("error", "no_annual_filing"):
            run.state(code, status, res["http_status"], res["note"], at)
            return status
        filing = res["filing"]
        primary = run.groups[code][0]
        partial = bool(res["truncated"])
        row = {"doc_id": f"{SOURCE_ID}:{code}:{Path(filing['filename']).stem}:{SECTION}",
               "security_id": primary["security_id"], "company_key": primary["company_key"], "source_id": SOURCE_ID,
               "cik": None, "form": FORM_LABEL, "section": SECTION, "accession": filing["filename"],
               "filing_date": filing["filing_date"], "report_date": filing["report_date"], "url": filing["list_url"],
               "raw_sha256": res["pdf_sha256"], "raw_bytes": res["pdf_bytes"], "text_path": None,
               "text_sha256": None, "text_chars": None,
               "extractor": EXTRACTOR_VERSION + (PARTIAL_SUFFIX if partial else ""), "extract_note": res["note"],
               "fetched_at": at}
        if status == "extract_failed":
            run.batch.docs.append(row)
            run.state(code, "extract_failed", 200, res["note"], at)
            return "extract_failed"
        tp = text_path_for(cfg, code, filing["filename"])
        tp.parent.mkdir(parents=True, exist_ok=True)
        data = res["text"].encode("utf-8")
        tp.write_bytes(data)
        row.update(text_path=str(tp), text_sha256=store.sha256(data), text_chars=len(res["text"]))
        run.batch.docs.append(row)
        if res["description"]:
            run.add_description(code, res["description"], "zh", filing["list_url"], at, ID_TYPE)
        run.state(code, "ok", 200, res["note"], at)
        summary["ok_documents"] += 1
        return "ok"

    def process_basic(code: str) -> str:
        at = store.now_utc()
        t = time.monotonic()
        try:
            res = fetch_basic(client, code, on_request=seen(code))
        finally:
            run.batch.duration_s += time.monotonic() - t
        summary["bytes_downloaded"] += len(res["body"])
        refused_or_reset(res)
        if res["status"] != "ok":
            run.state(code, res["status"], res["http_status"], res["note"], at)
            return res["status"]
        page = BASIC_PAGE_URL.format(code=code)
        run.add_aux(f"basic-{code}", page, res["body"], at, co_id=code)
        run.add_description(code, res["info"]["main_business"], "zh", page, at, ID_TYPE)
        run.state(code, "ok", 200, res["note"], at)
        summary["ok_descriptions"] += 1
        return "ok"

    def url_for(code: str) -> str:
        return current.get(code) or (LIST_URL.format(code=code, year=roc_year(today)) if mode == "annual"
                                     else BASIC_API_URL)

    run.run_queue(queue, process_annual if mode == "annual" else process_basic, url_for)
    return run.close()
