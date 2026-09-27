"""CNINFO (巨潮资讯) adapter: A-share universe lines -> orgId -> latest annual report PDF -> business section.

Source (provenance.py): 'cninfo_annual_report' (official-private). The section text is kept in local files under
<home>/docs/cninfo/<code>/ and never shipped; `documents` holds metadata only, `descriptions` a short excerpt.

Endpoints (captured 2026-09-26, fixtures tests/fixtures/cninfo_*):
- GET  http://www.cninfo.com.cn/new/data/szse_stock.json -> {'stockList': [{code, orgId, zwjc, category, pinyin}]}
  (all Shanghai, Shenzhen and Beijing stocks, categories A股 / B股 / CDR).
- POST http://www.cninfo.com.cn/new/hisAnnouncement/query (form: stock='<code>,<orgId>', tabName=fulltext,
  column=szse, category='category_ndbg_szsh;', pageSize=30, pageNum=1; headers X-Requested-With: XMLHttpRequest,
  Content-Type: application/x-www-form-urlencoded) -> {'announcements': [{announcementTitle, adjunctUrl,
  adjunctSize (KB), announcementTime (ms), announcementId, ...}]}.
- GET  http://static.cninfo.com.cn/<adjunctUrl> -> the PDF.

Rules implemented here:
- Universe lines on SSE / SZSE (and a Beijing venue if TradingView ever lists one: 'BJSE', or 'BSE' with a CN ISIN;
  TradingView's 'BSE' is Bombay) are mapped by 6-digit code to the stock list (identifiers id_type 'cninfo_orgid').
  A-share codes map to A股/CDR entries only; a B-share line (SSE 900xxx, SZSE 200xxx) maps to its B股 entry.
  Lines sharing an orgId (A + B share of one company) are fetched once.
- Latest annual report (parse_title / select_annual_reports): titles carry any company-name / short-name prefix
  ('富士康工业互联网股份有限公司2025年年度报告', '中国移动：2025年年度报告摘要', '建设银行2025年度报告'); accepted forms are
  '<year>年年度报告' / '<year>年度报告' / '<year>年年报' (full) and '...摘要', '<year>年度业绩公告（年度报告摘要）',
  '<year>年度业绩报告' (summary), with the year in Arabic or Chinese numerals ('二零二四', '二0二五'). English versions, H-share
  copies ('建设银行H股公告-2025年年度报告'), a subsidiary's report filed by the parent ('中国平安：平安银行股份有限公司
  2025年年度报告摘要'), correction notices, cancelled ('已取消') and superseded ('更正前') copies are ignored; within a
  year a revision ('修订后' / '更正后' / '更新后' / '修订版') supersedes the original. The newest fiscal year among all
  reports and summaries wins; when a newer annual-report-like title cannot be parsed nothing is selected (never an
  older year) and the note is 'newest_year_unparsed:<year>' (a colon title naming the company's own full name,
  '中国平安：中国平安保险（集团）股份有限公司2025年年度报告', counts for this guard; 业绩快报 / 业绩预告 / 更正公告 /
  监管公告 copies do not). 'stale:<years>' is flagged when the year is behind the
  newest one that must be out (30 April deadline). kind='summary' tries [summary, full] (the full report when there
  is no summary, the summary's business section cannot be extracted, or it came only from the management-discussion
  fallback: both texts are then kept); kind='full' tries the full report (the summary when the year has none).
- Business section (extract_section): template rules of the report kind, then of the other kind, then heading
  families of banks / insurers / A+H issuers ('主要业务简介', '报告期主要业务', '业务综述', '业务回顾', '业务概览', ...,
  including chapters that exist only as running page headers), a too-long family section cut to MAX_SECTION_CHARS,
  and finally the opening of the '管理层讨论与分析' / '经营情况讨论与分析' chapter ('fallback:mda_opening', at most
  MDA_OPENING_CHARS).
- Two modes. Fast (default): (a) the bulk listing - the date-range query WITHOUT a stock (stock='', seDate
  '<since>~<today>', default since = 13 months ago but never later than settle_floor: 1 January of last year until
  30 April) on rate_key 'cninfo' (>= 1.0 s apart), every page saved raw, one 'list_pass' snapshot per pass. CNINFO
  serves at most PAGE_CAP_PAGES = 100 pages (3,000 rows) per query (live 2026-09-26: 11,403 rows reported, page 101
  = page 1), so list_range reads the range in adaptive date windows: a window whose page 1 reports more than
  WINDOW_MAX_ROWS (2,800) is split in half by date (down to single days), the others are paged to their end
  (list_window: hasMore=false, a short / empty page, a page of already-seen rows, the page cap or guard; complete
  only when it saw as many distinct rows as its page 1 reported). An incomplete leaf is repaired first: rows
  missing -> paged once more, rows united; a single day over the cap ('day_over_cap:<date>') -> re-listed per
  exchange ('plate' sh / sz / bj, verified live 2026-09-27, self-checking). A leaf still incomplete is a gap; an HTTP error
  / bad JSON / the page guard, or split halves whose page-1 totals do not add up to their window's (seDate taken
  as inclusive by China date, verified live 2026-09-27), make the pass unusable. A queue of at most LIST_MIN_QUEUE companies or a codes
  filter skips the listing. Rows are grouped
  by orgId and secCode; a company whose newest in-window year Y is complete in the window (since <= 1 Jan Y+1: every
  report of year Y was published inside it) and outside every gap (no gap ends on or after 1 Jan Y+1) gets exactly
  the per-company selection (same select_annual_reports on the same year's rows). Before any per-company query, a
  company whose stored section (current EXTRACTOR_VERSION; for kind='full' a full report) is of the newest fiscal
  year that can exist (last year) is 'skipped_current' without a request (unless refresh or codes). Other companies fall
  back to the per-company query (rate_key 'cninfo'): those a gap could affect without a cap, the rest (nothing in
  the window, or only an older year) at most max_fallback per run (known no-report companies last; Beijing
  companies outside the cap when the listing has no Beijing row); the rest are counted
  'no_annual_report_in_window', left without a crawl_state row, and make the run 'partial' above SKIPPED_WARN_SHARE
  of the queue. An unusable listing is not used at all: every company that is not current gets its query, no cap. (b) PDFs go through a separate limiter, rate_key 'cninfo-static' (static.cninfo.com.cn):
  a worker pool of daemon threads (DEFAULT_WORKERS 3, 1..MAX_WORKERS 4) fetches + extracts (summary-then-full
  fallback inside one task; PDF parsing serialised by _PDF_LOCK, PyMuPDF is not thread-safe), starts >= STATIC_MIN_INTERVAL_S apart and <= STATIC_MAX_PER_WINDOW per rolling second; the main thread owns
  the queue, the per-company fallback queries, the batch, the counters, the flushes (short sessions) and every stop
  decision. Per-company (per_company=True, the old path): one query + the PDFs per company, all on rate_key
  'cninfo' at >= 1.0 s, sequential. PDFs capped at MAX_PDF_BYTES, total deadline PDF_DEADLINE_S per attempt (query
  POST QUERY_DEADLINE_S). A timeout / network error on the full report after a kept fallback summary keeps the
  summary ('full_network:<Type>'). No DB connection is held during network I/O: every DB touch is a short
  store.session() per batch.
- A 200 PDF response that is an HTML / JSON / text page is a soft block (http.Blocked reason 'non_pdf_body');
  other non-PDF bytes are crawl_state 'error' note 'pdf_not_pdf', never a failed extraction.
- http.Blocked stops the whole run at once: http.Client sets its halt Event (shared by the 'cninfo' and
  'cninfo-static' clients, so no new request starts on either key), the cooldown marker (guard.mark_blocked, command
  'sync-cninfo') is written before any DB write - by the worker thread that saw it in fast mode - and on_blocked(exc)
  is called; in-flight PDFs drain (bounded), the batch is flushed and one '[cninfo] BLOCKED url=... status=...
  reason=...' line goes to stderr. 5 consecutive errors stop it; Ctrl-C / SIGTERM (converted to KeyboardInterrupt by
  the CLI) halts, drains, flushes and records 'interrupted'. Missing values stay NULL.
- Incremental: a report whose announcement was already processed (documents row with the same url) is not
  downloaded again unless refresh=True; in fast mode that is decided before any PDF task is submitted.
"""
from __future__ import annotations

import bisect
import collections
import datetime as dt
import http.client
import io
import itertools
import json
import math
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
from ..http import Blocked, Client, Halted, Response
from .edinet import sigterm_ignored

SOURCE_ID = "cninfo_annual_report"
COMMAND = "sync-cninfo"
STOCK_LIST_URL = "http://www.cninfo.com.cn/new/data/szse_stock.json"
QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
STATIC_URL = "http://static.cninfo.com.cn/{adjunct}"
RATE_KEY = "cninfo"                 # www.cninfo.com.cn: stock list, listing pages, per-company queries
DEFAULT_MIN_INTERVAL_S = 1.0
STATIC_RATE_KEY = "cninfo-static"   # static.cninfo.com.cn (PDF CDN), fast mode only
STATIC_MIN_INTERVAL_S = 0.25        # PDF starts at least this far apart ...
STATIC_MAX_PER_WINDOW = 4           # ... and at most this many in any rolling second
DEFAULT_WORKERS = 3                 # fast mode PDF workers (fetch + extract)
MAX_WORKERS = 4
LIST_WINDOW_MONTHS = 13             # default bulk listing window: 13 months back from today
MAX_FALLBACK_QUERIES = 300          # fast mode: per-company queries for companies not settled by the listing
LIST_MIN_QUEUE = 400                # fast mode: a queue this small (or --codes) skips the listing (~420 requests)
SKIPPED_WARN_SHARE = 0.05           # more companies than this share left unqueried past the cap -> run 'partial'
MAX_LIST_PAGES = 2_000              # hard guard on the listing requests of one pass (13 months: ~420 pages)
PAGE_CAP_PAGES = 100                # CNINFO serves at most 100 pages per query (live 2026-09-26: page 101 == page 1)
PAGE_CAP_ROWS = PAGE_CAP_PAGES * 30  # = 3,000 rows per query (pageSize 30)
WINDOW_MAX_ROWS = 2_800             # a listing window reporting more rows on page 1 is split in half by date
SPLIT_SLACK_DOWN = 3                # split check: children may report at most this many rows fewer than the parent
SPLIT_SLACK_UP_MIN = 30             # ... and at most max(this, 1%) more (rows added meanwhile)
# a single day over the cap is re-listed per exchange with the query's 'plate' filter (live probe 2026-09-27:
# 2026-04-29 = 1,424 rows = sh 367 + sz 1,006 + bj 51, each plate only its own market's codes; the repair stays
# self-checking - every plate must stay under the cap, hold only its own market's codes and the plate totals must
# add up to the day's total - and costs 1 request when the server ignores the filter)
LIST_PLATES = (("sh", "SSE"), ("sz", "SZSE"), ("bj", "BJ"))
RESULT_POLL_S = 0.5                 # main thread wakes at least this often while PDFs are in flight
DRAIN_MAX_S = 60.0                  # after a stop, wait at most this long for in-flight PDFs (then abandon them:
                                    # the workers are daemon threads, so process exit does not wait for them)
ID_TYPE = "cninfo_orgid"
EXTRACTOR_VERSION = "cninfo-v3"   # v3: bank / insurer / A+H heading families, running headers, MD&A
                                 # fallback (v2: 'N.M' STAR summary headings); failed rows of older versions
                                 # are retried
SECTION = "business"
QUERY_COLUMN = "szse"                 # the query's 'column' for all A-share markets (SH / SZ / BJ)
QUERY_CATEGORY = "category_ndbg_szsh;"   # annual reports
QUERY_PAGE_SIZE = 30
QUERY_HEADERS = {"X-Requested-With": "XMLHttpRequest",
                 "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                 "Accept": "application/json, text/javascript, */*; q=0.01"}

MIN_SECTION_CHARS = 200
MAX_SECTION_CHARS = 60_000
MAX_PDF_BYTES = 30 * 1024 * 1024
PDF_DEADLINE_S = 300.0          # http.Client total wall-clock time per PDF attempt (30 MB -> >= ~100 KB/s)
QUERY_DEADLINE_S = 60.0         # hisAnnouncement/query POST (a few KB of JSON)
SHORT_DESC_CHARS = 1_200
MAX_CONSECUTIVE_ERRORS = 5
FLUSH_WAIT_S = 120.0
FINAL_WAIT_S = 600.0
MAX_LOCKED_FLUSHES = 5
MAX_PENDING = 500
CN_TZ = dt.timezone(dt.timedelta(hours=8))

CRAWL_STATUSES = ("ok", "no_annual_report", "extract_failed", "error", "blocked")
KINDS = ("summary", "full")
FORM_FOR_KIND = {"summary": "annual_report_summary", "full": "annual_report"}

# TradingView exchange codes for mainland venues. 'BSE' is Bombay on TradingView: accepted only with a CN ISIN.
CN_VENUES = frozenset({"SSE", "SZSE", "BJSE"})
_CODE_RE = re.compile(r"^\d{6}$")


# =========================================================================== stock list -> orgId


def parse_stock_list(payload: Any) -> list[dict]:
    """szse_stock.json ({'stockList': [...]}) or a plain list -> [{code, org_id, name, category}] (6-digit codes).
    Idempotent: already-parsed rows are accepted too."""
    if isinstance(payload, (bytes, str)):
        payload = json.loads(payload)
    rows = payload.get("stockList") if isinstance(payload, Mapping) else payload
    out = []
    for r in rows or []:
        if not isinstance(r, Mapping):
            continue
        code, org = str(r.get("code") or "").strip(), str(r.get("orgId") or r.get("org_id") or "").strip()
        if not _CODE_RE.match(code) or not org:
            continue
        out.append({"code": code, "org_id": org, "name": r.get("zwjc", r.get("name")),
                    "category": str(r.get("category") or "")})
    return out


def market_of_code(code: str) -> str | None:
    """Venue family of a 6-digit mainland code: 'SSE' (6xxxxx, 9xxxxx B), 'SZSE' (0/2/3xxxxx), 'BJ' (4/8xxxxx,
    92xxxx). 900xxx is an SSE B share, 920xxx a Beijing share."""
    if not _CODE_RE.match(code or ""):
        return None
    if code.startswith("92"):
        return "BJ"
    if code[0] in "69":
        return "SSE"
    if code[0] in "023":
        return "SZSE"
    if code[0] in "48":
        return "BJ"
    return None


def is_b_share(code: str) -> bool:
    return code.startswith("900") or code.startswith("200")


def _venue_family(exchange: str, isin: str | None) -> str | None:
    ex = (exchange or "").strip().upper()
    if ex in ("SSE", "SZSE"):
        return ex
    if ex == "BJSE" or (ex == "BSE" and str(isin or "").upper().startswith("CN")):
        return "BJ"
    return None


def is_cn_line(sec: Mapping[str, Any]) -> bool:
    return _venue_family(str(sec.get("exchange") or ""), sec.get("isin")) is not None


def map_securities_to_orgid(securities: Sequence[Mapping[str, Any]], stock_rows: Any, *,
                            report: dict | None = None) -> list[tuple[str, str, str, str]]:
    """Map mainland universe lines to CNINFO orgIds by 6-digit code. Returns [(security_id, org_id, code, method)].

    Only SSE / SZSE lines (and BJSE, or BSE with a CN ISIN) with a 6-digit symbol whose code family matches the
    venue are considered. A-share codes match A股 / CDR entries, B-share codes (900xxx / 200xxx) match B股 entries.
    A code with several distinct orgIds is left unmapped and listed in report['ambiguous'].
    `report` receives: considered, non_cn, unmatched, venue_mismatch, ambiguous (list)."""
    by_code: dict[str, dict[str, dict]] = {}
    for r in parse_stock_list(stock_rows):
        by_code.setdefault(r["code"], {}).setdefault(r["org_id"], r)
    rep = {"considered": 0, "non_cn": 0, "unmatched": 0, "venue_mismatch": 0, "ambiguous": []}
    out: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for sec in securities:
        sid = str(sec.get("security_id") or "")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        exch, _, sym = sid.partition(":")
        fam = _venue_family(str(sec.get("exchange") or exch), sec.get("isin"))
        code = str(sec.get("symbol") or sym).strip()
        if fam is None or not _CODE_RE.match(code):
            rep["non_cn"] += 1
            continue
        rep["considered"] += 1
        if market_of_code(code) != fam:
            rep["venue_mismatch"] += 1
            continue
        want_b = is_b_share(code)
        cands = {org: r for org, r in (by_code.get(code) or {}).items()
                 if (r["category"] == "B股") == want_b and (want_b or r["category"] in ("A股", "CDR", ""))}
        if not cands:
            rep["unmatched"] += 1
            continue
        if len(cands) > 1:
            rep["ambiguous"].append({"security_id": sid, "code": code, "org_ids": sorted(cands)})
            continue
        out.append((sid, next(iter(cands)), code, "code_exact"))
    if report is not None:
        report.update(rep)
    return out


# =========================================================================== announcements -> latest annual report

_TAG_RE = re.compile(r"<[^>]+>")
# fiscal year in Arabic ('2025') or Chinese numerals ('二零二五' / '二〇二五')
_CN_YEAR_DIGITS = {"零": 0, "〇": 0, "○": 0, "Ｏ": 0, "О": 0, "0": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                   "六": 6, "七": 7, "八": 8, "九": 9}      # ASCII '0' too: '二0二五'
_YEAR = r"(?:(?:19|20)\d{2}|[一二][零〇○ＯО0一二三四五六七八九]{3})"
# '<year>年年度报告' / '<year>年度报告' / '<year>年年报' / '<year>年报' / '<year>年度业绩公告' / '<year>年度业绩报告'
_TITLE_RE = re.compile(rf"(?P<year>{_YEAR})\s*年\s*(?:(?P<report>年?\s*度\s*报\s*告)|(?P<short>年?\s*报)(?!\s*告)"
                       r"|(?P<results>年?\s*度\s*业\s*绩\s*(?:公\s*告|报\s*告)))(?P<rest>.*)$")
# anything that looks like an annual report of some year (the newest-year guard; see annual_report_scan)
_LOOSE_TITLE_RE = re.compile(rf"(?P<year>{_YEAR})\s*年.{{0,3}}?(?:年报|度报告|度业绩)")
_PREFIX_REJECT = ("关于", "更正", "补充", "说明", "通知", "提示", "H股", "意见", "审核", "核查", "董事会", "监事会",
                  "独立", "半年", "季度", "问询", "回复", "英文", "取消", "公告", "摘要")
_REVISION_WORDS = ("更新", "修订", "更正", "补充", "修正")
# what may follow the report words (after '摘要'): only bracketed tags / 全文 / 正文 made of these words
_REST_ALLOWED = re.compile(r"^(?:全文|正文|\((?:[^()]{0,20})\)|[\s\-—_:：、]|english|version)*$", re.I)
# titles that are never the company's own Chinese annual report (H-share / English copies, notices, other reports)
_NOT_OWN_REPORT = ("h股", "英文", "english", "annual report", "半年", "季度", "关于", "提示", "意见", "审核", "核查",
                   "问询", "回复", "说明", "披露", "股东", "通知", "取消", "废止", "作废", "快报", "预告", "更正公告",
                   "监管公告")


def _year_value(s: str) -> int | None:
    s = re.sub(r"\s+", "", s)
    if s.isdigit():
        return int(s)
    digits = [_CN_YEAR_DIGITS.get(ch) for ch in s]
    if len(digits) != 4 or any(d is None for d in digits):
        return None
    return int("".join(str(d) for d in digits))


def _normalise_title(title: Any) -> str:
    t = _TAG_RE.sub("", str(title or ""))
    t = t.replace("（", "(").replace("）", ")").replace("【", "(").replace("】", ")").replace("：", ":")
    return re.sub(r"\s+", " ", t).strip()


def parse_title(title: Any) -> dict | None:
    """'2025年年度报告摘要（更新后）' -> {'year': 2025, 'kind': 'summary', 'english': False, 'cancelled': False,
    'revision': True}. None for anything that is not the company's own annual report or its summary (half-year /
    quarterly reports, correction notices, opinions on the report, H-share copies, a subsidiary's report, ...).

    Accepted (after any company-name / short-name prefix, e.g. '富士康工业互联网股份有限公司', '中国移动：'):
    '<year>年年度报告' / '<year>年度报告' / '<year>年年报' / '<year>年报' (full; '全文' / '正文' may follow),
    the same + '摘要' (summary), '<year>年度业绩公告（年度报告摘要）' / '<year>年度业绩报告' (summary). The year may
    be written in Chinese numerals ('二零二四', '二〇二五'). English copies are flagged 'english'; cancelled and
    superseded pre-revision copies ('（更正前）') 'cancelled'; '修订后' / '更正后' / '更新后' / '修订版' 'revision'."""
    t = _normalise_title(title)
    m = _TITLE_RE.search(t)
    if not m:
        return None
    year = _year_value(m.group("year"))
    if year is None or not 1990 <= year <= 2100:
        return None
    prefix, rest = t[:m.start()].strip(), m.group("rest").strip()
    low = t.lower()
    if "h股" in low or "annual report" in low:
        return None                                   # H-share copies ('建设银行H股公告-2025年年度报告')
    if len(prefix) > 40 or any(w in prefix for w in _PREFIX_REJECT):
        return None
    if ":" in prefix and prefix.split(":", 1)[1].strip(" -—_"):
        return None                                   # another company's report ('中国平安：平安银行股份有限公司…')
    kind = "full"
    if m.group("results"):
        kind = "summary"                              # '二零二四年度业绩公告（年度报告摘要）'
    if rest.startswith("摘要"):
        kind, rest = "summary", rest[2:].strip()
    elif rest.startswith("(") and "摘要" in rest[:12] and not m.group("results"):
        kind = "summary"                              # '2025年年度报告(摘要)'
    english = "英文" in rest or "english" in rest.lower()
    # '（已取消）' / '（已废止）' / '（作废）' copies, and the superseded pre-revision copies ('（更正前）', '（修订前）',
    # '（更新前）') re-titled on the day the revision is posted, are dropped like cancelled ones
    superseded = any(w + "前" in rest for w in _REVISION_WORDS)
    cancelled = any(w in rest for w in ("取消", "废止", "作废")) or superseded
    revision = not superseded and any(w in rest for w in _REVISION_WORDS)
    if not _REST_ALLOWED.match(rest):
        return None
    return {"year": year, "kind": kind, "english": english, "cancelled": cancelled, "revision": revision}


def looks_like_annual_report(title: Any) -> int | None:
    """Fiscal year of a title that looks like the company's own annual report or summary even when parse_title
    cannot read it ('XX2025年年度报告及摘要'), else None. H-share / English copies, notices about a report,
    half-year / quarterly reports and cancelled copies are not annual-report-like."""
    t = _normalise_title(title)
    m = _LOOSE_TITLE_RE.search(t)
    if not m:
        return None
    low = t.lower()
    if any(w in low for w in _NOT_OWN_REPORT) or re.search(r"(?:更正|修订|更新|补充|修正)前", t):
        return None
    prefix = t[:m.start()]
    if ":" in prefix:
        short, after = (x.strip(" -—_") for x in prefix.split(":", 1))
        short = re.sub(r"^\*?ST|\s+", "", short)
        # '中国平安：平安银行股份有限公司…' names another entity; '中国平安：中国平安保险(集团)股份有限公司…' (the
        # company's own full name contains its short name) may be its own report, so it counts for the guard
        if after and not (short and short in after):
            return None
    return _year_value(m.group("year"))


def _ms_to_date(ms: Any) -> dt.date | None:
    try:
        return dt.datetime.fromtimestamp(int(ms) / 1000, tz=CN_TZ).date()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def pdf_url(adjunct_url: str) -> str:
    return STATIC_URL.format(adjunct=str(adjunct_url).lstrip("/"))


def annual_report_candidates(announcements: Iterable[Mapping[str, Any]] | None) -> list[dict]:
    """Every usable annual report / summary in a query response (English, cancelled, non-PDF entries dropped)."""
    return annual_report_scan(announcements)[0]


_OWN_CODE_PREFIX_RE = re.compile(r"^\s*(\d{6})\s*[:：]\s*")


def title_of(a: Mapping[str, Any]) -> Any:
    """The row's announcementTitle without a leading '<its own secCode>：' ('603629：利通电子2025年年度报告' ->
    '利通电子2025年年度报告'). Only the row's own 6-digit code is stripped: '中国平安：平安银行…' (another company's
    report) and a different code stay as they are and are rejected by parse_title."""
    title = a.get("announcementTitle")
    m = _OWN_CODE_PREFIX_RE.match(str(title or ""))
    if m and m.group(1) == str(a.get("secCode") or "").strip():
        return str(title)[m.end():]
    return title


def annual_report_scan(announcements: Iterable[Mapping[str, Any]] | None) -> tuple[list[dict], list[dict]]:
    """(usable candidates, unparsed annual-report-like entries [{'year', 'title'}]) of a query response."""
    out, unparsed = [], []
    for a in announcements or []:
        if not isinstance(a, Mapping):
            continue
        title = title_of(a)
        p = parse_title(title)
        adj = str(a.get("adjunctUrl") or "").strip()
        if p is None:
            y = looks_like_annual_report(title)
            if y is not None:
                unparsed.append({"year": y, "title": _TAG_RE.sub("", str(a.get("announcementTitle") or "")).strip()})
            continue
        if p["english"] or p["cancelled"] or not adj:
            continue
        if not adj.lower().endswith(".pdf") and str(a.get("adjunctType") or "").upper() not in ("PDF", ""):
            continue
        try:
            size_kb = int(a.get("adjunctSize")) if a.get("adjunctSize") is not None else None
        except (TypeError, ValueError):
            size_kb = None
        out.append({"title": _TAG_RE.sub("", str(a.get("announcementTitle") or "")).strip(), "year": p["year"],
                    "kind": p["kind"], "revision": p["revision"],
                    "announcement_id": str(a.get("announcementId") or "") or Path(adj).stem,
                    "adjunct_url": adj, "url": pdf_url(adj), "size_kb": size_kb,
                    "time_ms": int(a.get("announcementTime") or 0) if str(a.get("announcementTime") or "0").isdigit()
                    else 0, "filing_date": _ms_to_date(a.get("announcementTime"))})
    return out, unparsed


def expected_latest_year(as_of: dt.date) -> int:
    """Newest fiscal year whose annual report must be out by `as_of` (mainland deadline: 30 April)."""
    return as_of.year - 1 if (as_of.month, as_of.day) > (4, 30) else as_of.year - 2


def newest_possible_year(as_of: dt.date) -> int:
    """Newest fiscal year whose annual report can exist on `as_of`: last year, on every day of the year (after 30
    April it is also the expected one; from January to 30 April, FY last year is being published while FY the year
    before is the expected one). A company holding a report of this year cannot have a newer one."""
    return as_of.year - 1


def _today() -> dt.date:
    """Today in China (the listing's end date and the clock of the pre-query skip; patched by tests)."""
    return dt.datetime.now(CN_TZ).date()


def select_annual_reports(announcements: Iterable[Mapping[str, Any]] | None, kind: str = "summary", *,
                          as_of: dt.date | None = None, report: dict | None = None) -> list[dict]:
    """Ordered documents to try for the newest fiscal year: kind='summary' -> [summary, full] (either may be
    absent), kind='full' -> [full] (or [summary] when the newest year has no full report).

    The newest fiscal year among all usable reports and summaries wins; within a year and kind a revision
    ('修订后' / '更正后' / '更新后') supersedes the original, then the latest announcement, then the higher id.
    Guard: when an annual-report-like title of a NEWER year could not be parsed, nothing is returned (never an
    older year) and report['note'] = 'newest_year_unparsed:<year>'. Each dict gets 'flags': 'stale:<years>' when the
    year is behind the newest one that must be published by `as_of` (expected_latest_year), 'summary_only' when
    kind='full' falls back to the summary."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, not {kind!r}")
    cands, unparsed = annual_report_scan(announcements)
    if report is not None:
        report["note"] = None
    newest_unparsed = max((u["year"] for u in unparsed), default=None)
    year = max((c["year"] for c in cands), default=None)
    if newest_unparsed is not None and (year is None or newest_unparsed > year):
        if report is not None:
            report["note"] = f"newest_year_unparsed:{newest_unparsed}"
            report["unparsed_titles"] = [u["title"] for u in unparsed if u["year"] == newest_unparsed]
        return []
    if year is None:
        return []
    today = as_of or dt.datetime.now(CN_TZ).date()
    age = (today - dt.date(year, 12, 31)).days / 365.25
    flags = [f"stale:{max(1, int(age))}"] if year < expected_latest_year(today) else []
    by_kind = {k: [c for c in cands if c["year"] == year and c["kind"] == k] for k in KINDS}
    wanted = ("summary", "full") if kind == "summary" else (("full",) if by_kind["full"] else ("summary",))
    out = []
    for k in wanted:
        if by_kind[k]:
            best = max(by_kind[k], key=lambda c: (c["revision"], c["time_ms"], c["announcement_id"]))
            extra = ["summary_only"] if kind == "full" and k == "summary" else []
            out.append(dict(best, flags=list(flags) + extra))
    return out


# =========================================================================== PDF -> text


class PdfError(RuntimeError):
    """The PDF could not be parsed by any available backend."""


def pdf_backends() -> list[str]:
    out = []
    try:
        import fitz  # noqa: F401  (PyMuPDF)
        out.append("pymupdf")
    except ImportError:
        pass
    try:
        import pypdf  # noqa: F401
        out.append("pypdf")
    except ImportError:
        pass
    return out


def _pages_pymupdf(data: bytes) -> list[str]:
    import fitz
    try:
        fitz.TOOLS.mupdf_display_errors(False)
    except Exception:  # older/newer PyMuPDF without the switch
        pass
    with fitz.open(stream=data, filetype="pdf") as doc:
        return [page.get_text("text") or "" for page in doc]


def _pages_pypdf(data: bytes) -> list[str]:
    import logging
    import pypdf
    log = logging.getLogger("pypdf")
    level = log.level
    log.setLevel(logging.ERROR)     # malformed-PDF warnings are expected here; the caller records a note
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        return [(page.extract_text() or "") for page in reader.pages]
    finally:
        log.setLevel(level)


_PDF_LOCK = threading.Lock()   # PyMuPDF does not support multithreading; pypdf's logger level is process-global


def pdf_pages(data: bytes, *, backend: str = "auto") -> tuple[list[str], str]:
    """Text of each page and the backend used: PyMuPDF first, pypdf as the fallback ('auto'), or the one named.
    Serialised by a module lock: fast mode downloads PDFs concurrently but parses one at a time (PyMuPDF is not
    thread-safe; parsing takes ~0.08 s against ~2 s per download)."""
    with _PDF_LOCK:
        return _pdf_pages_locked(data, backend)


def _pdf_pages_locked(data: bytes, backend: str) -> tuple[list[str], str]:
    order = pdf_backends() if backend == "auto" else [backend]
    if not order:
        raise PdfError("no PDF backend installed (PyMuPDF or pypdf)")
    last: BaseException | None = None
    for name in order:
        try:
            pages = _pages_pymupdf(data) if name == "pymupdf" else _pages_pypdf(data)
        except ImportError as e:
            last = e
            continue
        except Exception as e:  # malformed PDF: try the next backend
            last = e
            continue
        if any(p.strip() for p in pages) or name == order[-1]:
            return pages, name
    raise PdfError(f"{type(last).__name__}: {last}" if last else "pdf_parse_failed")


# ---- cleaning

_CJK = r"⺀-⿿　-〿㐀-䶿一-鿿豈-﫿＀-￯"
_WS_RE = re.compile(r"[ \t\r\f\v  -​  　]+")
_ASCII_CJK_RE = re.compile(rf"(?<=[0-9A-Za-z%.,)\]])\s+(?=[{_CJK}])|(?<=[{_CJK}])\s+(?=[0-9A-Za-z%(\[])")
_PAGE_NO_RE = re.compile(r"^(?:[-—–_~\s]*\d{1,4}[-—–_~\s]*|第\s*\d{1,4}\s*页(?:\s*[/，,]?\s*共\s*\d{1,4}\s*页)?"
                         r"|\d{1,4}\s*/\s*\d{1,4}|[ivxlcIVXLC]{1,6}|page\s*\d{1,4}(?:\s*of\s*\d{1,4})?)$", re.I)
HEADER_ZONE = 3


def normalise_line(line: str) -> str:
    """Collapse whitespace; drop the spaces PDF extraction puts between CJK text and digits/Latin ('2025 年' ->
    '2025年')."""
    s = _WS_RE.sub(" ", line.replace("­", "")).strip()
    return _ASCII_CJK_RE.sub("", s)


def _compact(line: str) -> str:
    return re.sub(r"\s+", "", line)


def _line_key(line: str) -> str:
    return re.sub(r"\d+", "#", _compact(line))


def is_page_number(line: str) -> bool:
    return bool(_PAGE_NO_RE.match(line.strip()))


def clean_pages(pages: Sequence[str], zone: int = HEADER_ZONE, *, offsets: list[int] | None = None) -> list[str]:
    """Page texts -> one list of non-empty, normalised lines without running headers/footers.

    Within the first/last `zone` lines of each page, a contiguous run of page-number lines ('12', '- 12 -',
    '第12页 共40页', '12/40') and of lines repeated (digits ignored) in the same zone on at least half of the pages
    (min. 2) is dropped: the company-name + report-title header and similar footers. Lines ending like a sentence
    ('。', '；', ...) are never treated as headers. `offsets` (optional list) receives the index of each page's first
    output line, then the total."""
    page_lines = [[ln for ln in (normalise_line(x) for x in (p or "").splitlines()) if ln] for p in pages]
    n = len(page_lines)
    counts: collections.Counter = collections.Counter()
    for lines in page_lines:
        counts.update({_line_key(ln) for ln in lines[:zone] + lines[-zone:]})
    threshold = max(2, math.ceil(n * 0.5)) if n >= 2 else math.inf

    def junk(ln: str) -> bool:
        # a sentence (ends with 。/；/！/？) is body text even when it repeats: headers/footers are labels
        return is_page_number(ln) or (counts[_line_key(ln)] >= threshold and not re.search(r"[。；！？]$", ln))

    out: list[str] = []
    for lines in page_lines:
        if offsets is not None:
            offsets.append(len(out))
        lo, hi = 0, len(lines)
        while lo < min(zone, hi) and junk(lines[lo]):
            lo += 1
        while hi > max(lo, len(lines) - zone) and junk(lines[hi - 1]):
            hi -= 1
        out.extend(lines[lo:hi])
    if offsets is not None:
        offsets.append(len(out))
    return out


# ---- headings

_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_NUM_PREFIX_RE = re.compile(
    r"^(?:(?P<cn>[一二三四五六七八九十]{1,3})[、.．]"
    rf"|(?P<sub>\d{{1,2}})\.(?P<subn>\d{{1,2}})(?:[、．]|\.(?!\d))?(?=[{_CJK}])"
    rf"|(?P<ar>\d{{1,2}})(?:[、．]|\.(?!\d)|(?=[{_CJK}]))"
    r"|[（(](?P<pn>[一二三四五六七八九十]{1,3}|\d{1,2})[）)])")
_UNIT_START = re.compile(r"^[万亿千百十元美港日欧吨倍个份次家人年月日周天台套辆股%％℃米克升]")
_PART_RE = re.compile(r"^第[一二三四五六七八九十]{1,3}[节章]")
_TOC_RE = re.compile(r"\.{4,}|…{2,}|·{4,}|\.{3,}\s*\d{1,4}\s*$")
_SENTENCE_PUNCT = re.compile(r"[。；！？]|，.*，")
MAX_HEADING_CHARS = 45


def cn_number(s: str) -> int | None:
    """'三' -> 3, '十二' -> 12, '二十' -> 20."""
    if not s:
        return None
    if len(s) == 1:
        return _CN_DIGITS.get(s)
    if s.startswith("十"):
        return 10 + (_CN_DIGITS.get(s[1:], 0) if len(s) > 1 else 0)
    if len(s) >= 2 and s[1] == "十":
        return _CN_DIGITS.get(s[0], 0) * 10 + (_CN_DIGITS.get(s[2:], 0) if len(s) > 2 else 0)
    return None


def parse_heading(line: str) -> dict | None:
    """A heading-like line -> {'level': 'part'|'cn'|'ar'|'sub'|'paren'|'bare', 'n': ordinal|None, 'title': str}.
    'sub' is the 'N.M' numbering of the STAR-market summary template ('2.2 报告期公司主要业务简介'): n = M, 'major'
    = N.

    Headings are short (<= 45 chars without spaces), carry no sentence punctuation, and are not table-of-contents
    rows (dot leaders + page number). Unnumbered lines count only when <= 30 chars and free of commas."""
    c = _compact(line)
    if not c or len(c) > MAX_HEADING_CHARS or _TOC_RE.search(line):
        return None
    if _PART_RE.match(c):
        return {"level": "part", "n": cn_number(re.match(r"^第([一二三四五六七八九十]{1,3})", c).group(1)),
                "title": c}
    m = _NUM_PREFIX_RE.match(c)
    title = c[m.end():] if m else c
    if not title or _SENTENCE_PUNCT.search(title) or title.endswith(("，", ",")):
        return None
    if m is None:
        if len(title) > 30 or "，" in title:
            return None
        return {"level": "bare", "n": None, "title": title}
    if m.group("cn"):
        return {"level": "cn", "n": cn_number(m.group("cn")), "title": title}
    if m.group("sub"):
        if _UNIT_START.match(title) or int(m.group("subn")) > 30:
            return None   # a decimal amount ('2.57亿元'), not a heading
        return {"level": "sub", "n": int(m.group("subn")), "major": int(m.group("sub")), "title": title}
    if m.group("ar"):
        if not re.match(rf"[{_CJK}]", title):
            return None
        return {"level": "ar", "n": int(m.group("ar")), "title": title}
    return {"level": "paren", "n": None, "title": title}


_START_RULES = {
    "summary": re.compile(r"^报告期内?(?:公司)?(?:的)?主要业务(?:或产品)?(?:简介|情况|概要)"),
    "full": re.compile(r"^报告期内?(?:公司)?(?:所)?从事的(?:主要)?业务"),
}
_NAMED_ENDS = {
    "summary": [("accounting_data", re.compile(r"^(?:公司)?(?:近三年)?主要会计数据"))],
    "full": [("core_competence", re.compile(r"^(?:报告期内)?(?:公司)?(?:的)?核心竞争力")),
             ("core_tech", re.compile(r"^核心技术与研发进展")),
             ("main_business_analysis", re.compile(r"^主营业务分析")),
             ("operations", re.compile(r"^报告期内(?:公司)?(?:的)?主要经营情况")),
             ("major_assets", re.compile(r"^主要资产重大变化情况"))],
}


def _find_end(heads: list[tuple[int, dict]], start_idx: int, start_head: dict, kind: str) -> list[tuple[int, str]]:
    """End candidates for a start line, preferred first: the nearest named heading (or '第X节'), then the nearest
    structural 'next item' (a following top-level item)."""
    named: tuple[int, str] | None = None
    nxt: tuple[int, str] | None = None
    for i, h in heads:
        if i <= start_idx:
            continue
        if named is None:
            if h["level"] == "part":
                named = (i, "next_part")
            elif h["level"] != "bare" or kind == "full":
                for name, rx in _NAMED_ENDS[kind]:
                    if rx.match(h["title"]):
                        named = (i, name)
                        break
        if nxt is None:
            if kind == "summary":
                if h["level"] == "cn" or (h["level"] == "ar" and start_head["level"] == "ar"
                                          and (h["n"] or 0) > (start_head["n"] or 0)):
                    nxt = (i, "next_item")
                elif start_head["level"] == "sub" and (
                        (h["level"] == "sub" and h.get("major") == start_head.get("major")
                         and (h["n"] or 0) > (start_head["n"] or 0))
                        or (h["level"] == "ar" and (h["n"] or 0) > (start_head.get("major") or 0))):
                    nxt = (i, "next_item")
            elif h["level"] == "cn" and start_head["level"] == "cn" and (h["n"] or 0) > (start_head["n"] or 0):
                nxt = (i, "next_item")
        if named is not None and nxt is not None:
            break
    return [c for c in (named, nxt) if c is not None]


def join_paragraphs(lines: Sequence[str]) -> str:
    """Re-flow PDF lines into paragraphs (one per output line).

    A line ends its paragraph when it ends with sentence punctuation or a colon, is clearly shorter than a full
    line, or is a numbered heading; a numbered line or a bullet always starts a new paragraph. CJK text joins without a space, Latin with one."""
    if not lines:
        return ""

    def width(s: str) -> int:
        return sum(2 if re.match(rf"[{_CJK}]", ch) else 1 for ch in s)

    widths = sorted(width(ln) for ln in lines if width(ln) > 20)
    full = widths[int(len(widths) * 0.9)] if widths else 0
    paras: list[str] = []
    cur = ""
    ended = True
    for ln in lines:
        head = parse_heading(ln)
        numbered = head is not None and head["level"] != "bare"
        pm = _NUM_PREFIX_RE.match(_compact(ln))
        starts_new = numbered or bool(pm and not pm.group("sub")) or ln[0] in "□√■●◆•"
        if cur and (ended or starts_new):
            paras.append(cur)
            cur = ""
        if cur:
            sep = " " if re.match(r"[0-9A-Za-z]", cur[-1]) and re.match(r"[0-9A-Za-z(]", ln[0]) else ""
            cur += sep + ln
        else:
            cur = ln
        ended = bool(re.search(r"[。！？；:：]$", ln)) or not full or width(ln) < full * 0.85 or numbered
    if cur:
        paras.append(cur)
    return "\n".join(paras)


def extract_business_section(lines: Sequence[str], kind: str) -> tuple[str | None, str]:
    """Business section of a cleaned annual report ('full') or summary ('summary').

    summary: from '报告期主要业务或产品简介' (also '报告期公司主要业务简介') to '主要会计数据...' (else the next
    top-level item: a following '三、', a higher '3、', or '第X节'). full: from '报告期内公司从事的主要业务' (also
    '...从事的业务情况' / '...所从事的主要业务、经营模式...') to '核心竞争力分析' (else '核心技术与研发进展',
    '主营业务分析', '报告期内主要经营情况', '主要资产重大变化情况' or '第X节'; last resort the next '三、'-level item).
    Starts must be heading lines (numbered or short, no sentence punctuation, not a TOC row). The re-flowed section
    must be MIN_SECTION_CHARS..MAX_SECTION_CHARS long. Returns (text, 'ok:end=<boundary>') or (None, reason)."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if not lines:
        return None, "empty_text"
    heads = [(i, h) for i, h in ((i, parse_heading(ln)) for i, ln in enumerate(lines)) if h is not None]
    starts = [(i, h) for i, h in heads if h["level"] != "part" and _START_RULES[kind].match(h["title"])]
    if not starts:
        return None, "start_heading_not_found"
    note = "end_heading_not_found"
    for s, sh in starts:
        for e, name in _find_end(heads, s, sh, kind):
            text = join_paragraphs(lines[s:e])
            if MIN_SECTION_CHARS <= len(text) <= MAX_SECTION_CHARS:
                return text, f"ok:end={name}"
            note = f"section_too_short:{len(text)}" if len(text) < MIN_SECTION_CHARS else \
                f"section_too_long:{len(text)}"
            if len(text) < MIN_SECTION_CHARS:
                break   # the fallback end is nearer or equal only by accident; a later start may do better
    return None, note


# ---- heading families of banks, insurers and A+H issuers (tried only when the template rules above fail)

MDA_OPENING_CHARS = 8_000
_PART_PREFIX_RE = re.compile(r"^第[一二三四五六七八九十]{1,3}[节章]")
_FAMILY_STARTS = {
    # '2.2主要业务简介' (CCB / ABC summaries), '二、报告期主要业务' (Ping An summary), '主营业务情况'
    "main_business": re.compile(r"^(?:报告期内?)?(?:公司)?(?:的)?主(?:要|营)业务(?:或产品)?"
                                r"(?:简介|情况|概要|概述|概况|介绍)?$"),
    # '8.3业务综述' (ICBC), '业务回顾' (BOC / CCB / PetroChina), '业务概览' (China Mobile), '3.3业务综述' (ABC)
    "business_review": re.compile(r"^(?:公司)?业务(?:综述|回顾|概览|概要|概况|概述)$"),
    # '报告期内公司所从事的主要业务…' variants outside the template wording
    "business_scope": re.compile(r"^报告期内?(?:公司)?(?:所)?(?:从事|经营)的?(?:主要|主营)?业务"),
    # older template chapter '第三节 公司业务概要'
    "company_business_summary": re.compile(r"^公司业务概要$"),
}
_FAMILY_ORDER = {"summary": ("main_business", "business_review", "business_scope", "company_business_summary"),
                 "full": ("business_review", "business_scope", "company_business_summary", "main_business")}
# opening of the management discussion chapter: '管理层讨论与分析', '第三节 经营情况讨论与分析', '8.讨论与分析',
# '5.1经营情况概览' (ICBC summary)
_MDA_START = re.compile(r"^(?:(?:经营情况|管理层)?讨论与分析|经营情况(?:概览|概述|综述|回顾))$")
# unnumbered end headings (whole line) of an unnumbered start
_BARE_END_RULES = [
    ("financial_review", re.compile(r"^(?:综合)?财务(?:概览|回顾|概要|摘要|报表分析)$")),
    ("accounting_data", re.compile(r"^(?:公司)?(?:近三年)?主要会计数据(?:和|及)?(?:主要)?(?:财务指标)?(?:摘要)?$")),
    ("mda", re.compile(r"^(?:经营情况|管理层)?讨论与分析$")),
    ("risk_management", re.compile(r"^风险(?:管理|因素)$")),
    ("capital_management", re.compile(r"^资本管理$")),
    ("outlook", re.compile(r"^(?:未来)?(?:发展)?展望$")),
    ("important_matters", re.compile(r"^重要事项$")),
    ("core_competence", re.compile(r"^(?:报告期内)?(?:公司)?核心竞争力(?:分析)?$")),
    ("shares", re.compile(r"^(?:股份|股本)变动及股东情况$")),
    ("governance", re.compile(r"^(?:公司治理|董事会报告|环境和社会责任|环境、社会及管治)$")),
]
_MDA_BARE_ENDS = ("important_matters", "shares", "governance")
_PAGE_INT_RE = re.compile(r"^[-—–_~\s]*(\d{1,3})[-—–_~\s]*$")
RUNNING_ZONE = 5          # lines at the top / bottom of a page searched for running headers
RUNNING_MIN_PAGES = 3


def _page_int(line: str) -> int | None:
    m = _PAGE_INT_RE.match(line)
    return int(m.group(1)) if m and int(m.group(1)) > 0 else None


class Layout:
    """Page structure of a cleaned report: offsets[p] = index of page p's first line in the cleaned lines (plus the
    total at the end); running = running header / footer labels left in the text by clean_pages (repeated on too
    few pages to be removed there: '管理层讨论与分析' + '业务回顾' on each page of that chapter, a company line
    missing from the financial statements pages); page_labels[p] = the running labels in page p's top / bottom
    RUNNING_ZONE lines."""

    def __init__(self, offsets: Sequence[int], running: set[str], page_labels: Sequence[set[str]]):
        self.offsets, self.running, self.page_labels = list(offsets), set(running), list(page_labels)

    @classmethod
    def from_pages(cls, pages: Sequence[str], offsets: Sequence[int]) -> "Layout":
        """Running labels: short (<= 40 chars) non-sentence, non-page-number lines found in the top / bottom
        RUNNING_ZONE lines of at least RUNNING_MIN_PAGES pages."""
        zones = []
        for pg in pages:
            ls = [ln for ln in (normalise_line(x) for x in (pg or "").splitlines()) if ln]
            zones.append({ln for ln in ls[:RUNNING_ZONE] + ls[-RUNNING_ZONE:]
                          if len(_compact(ln)) <= 40 and _page_int(ln) is None and not _SENTENCE_PUNCT.search(ln)})
        counts = collections.Counter(ln for z in zones for ln in z)
        running = {ln for ln, c in counts.items() if c >= RUNNING_MIN_PAGES}
        return cls(offsets, running, [z & running for z in zones])

    def page_of(self, line_idx: int) -> int:
        return max(0, bisect.bisect_right(self.offsets, line_idx) - 1)


def _is_chapter_label(label: str) -> bool:
    """A running label naming a chapter ('业务回顾'), not the company / report line or a table caption."""
    return len(_compact(label)) <= 20 and not re.search(r"年度?报告|年报|单位|[0-9]", label) and \
        parse_heading(label) is not None


def _family_title(h: dict) -> str:
    return _PART_PREFIX_RE.sub("", h["title"]) if h["level"] == "part" else h["title"]


def _family_end(heads: list[tuple[int, dict]], start_idx: int, sh: dict,
                bare_ends: Sequence[str] | None = None) -> tuple[int, str] | None:
    """Nearest end of a family section: '第X节' (next_part) for any start; for a numbered start the next item of
    the same or a higher numbering level with a plausible ordinal (at most 3 above; 'N.M' -> 'N+1、'), i.e.
    next_item; for an unnumbered start the next unnumbered chapter-level heading of _BARE_END_RULES (named),
    limited to `bare_ends` names when given."""
    lvl = sh["level"]
    n, major = sh.get("n") or 0, sh.get("major") or 0
    for i, h in heads:
        if i <= start_idx:
            continue
        hl, hn = h["level"], h.get("n") or 0
        if hl == "part":
            return i, "next_part"
        if lvl == "part":
            continue
        if lvl == "bare":
            if hl == "bare":
                for name, rx in _BARE_END_RULES:
                    if (bare_ends is None or name in bare_ends) and rx.match(h["title"]):
                        return i, name
            continue
        if hl == "ar" and _UNIT_START.match(h["title"]):
            continue                                   # '49个国家和地区…' is a sentence fragment, not '49、'
        if hl == "cn" and (lvl != "cn" or n < hn <= n + 3):
            return i, "next_item"
        if lvl == "ar" and hl == "ar" and n < hn <= n + 3:
            return i, "next_item"
        if lvl == "sub" and ((hl == "sub" and h.get("major") == major and n < hn <= n + 3)
                             or (hl == "sub" and major < (h.get("major") or 0) <= major + 2)
                             or (hl == "ar" and major < hn <= major + 2)):
            return i, "next_item"
        if lvl == "paren" and (hl in ("ar", "sub") or (hl == "paren" and n < hn <= n + 3)):
            return i, "next_item"
    return None


def _running_spans(lines: Sequence[str], layout: Layout, rx: re.Pattern) -> list[tuple[int, int, str, str]]:
    """Sections delimited by a running header whose label is a start heading (magazine-style reports where the
    chapter title only exists as the page header, e.g. CCB '业务回顾'): the largest run of pages carrying that
    label (gaps of at most 3 pages), extended back by one chapter-opening page without any chapter label, to the
    end of the run's last page."""
    out = []
    for label in layout.running:
        h = parse_heading(label)
        if h is None or not rx.match(_family_title(h)):
            continue
        pages = [p for p, labels in enumerate(layout.page_labels) if label in labels]
        runs: list[list[int]] = []
        for pg in pages:
            if runs and pg - runs[-1][-1] <= 3:
                runs[-1].append(pg)
            else:
                runs.append([pg])
        best = max(runs, key=len) if runs else []
        if len(best) < 2:
            continue
        first, last = best[0], best[-1]
        if first > 0 and not any(_is_chapter_label(lb) for lb in layout.page_labels[first - 1]):
            first -= 1                                 # the chapter's opening page has no running header
        s, e = layout.offsets[first], layout.offsets[min(last + 1, len(layout.offsets) - 1)]
        if e > s:
            out.append((s, e, "running_header", label))
    return out


def _section_text(lines: Sequence[str], s: int, e: int, running: set[str], heading: str) -> tuple[str, bool]:
    """Re-flowed text of lines[s:e] (heading first) without running headers and the page numbers next to them.
    Second value: the span looks like a table of contents (more than a quarter of its lines are page numbers)."""
    body = list(lines[s:e])
    if body and body[0] == heading:
        body = body[1:]
    drop = set()
    for k, ln in enumerate(body):
        if ln in running:
            drop.add(k)
            for d in range(-3, 4):
                if 0 <= k + d < len(body) and _page_int(body[k + d]) is not None:
                    drop.add(k + d)
    kept = [ln for k, ln in enumerate(body) if k not in drop]
    toc = len(body) >= 4 and sum(1 for ln in body if _page_int(ln) is not None) > len(body) / 4
    return join_paragraphs([heading] + kept), toc


def _clip(text: str, limit: int) -> str:
    """text cut to at most `limit` chars at a paragraph (else sentence) end."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    pos = cut.rfind("\n")
    if pos >= limit * 0.5:
        return cut[:pos]
    pos = max(cut.rfind(ch) for ch in "。；！？")
    return cut[:pos + 1] if pos >= limit * 0.5 else cut


def _family_sections(lines: Sequence[str], starts_rx: Sequence[tuple[str, re.Pattern]], layout: Layout | None, *,
                     bare_ends: Sequence[str] | None = None):
    """Yield (family, text | None, end_name, is_toc) for every start of each family in order (document order
    within a family: heading starts and running-header spans). Running labels are never headings."""
    running = layout.running if layout is not None else set()
    heads = [(i, h) for i, h in ((i, parse_heading(ln)) for i, ln in enumerate(lines) if ln not in running)
             if h is not None]
    for fam, rx in starts_rx:
        spans = []
        for i, h in heads:
            if (h["level"] == "part" and fam == "main_business") or not rx.match(_family_title(h)):
                continue
            end = _family_end(heads, i, h, bare_ends)
            spans.append((i, end[0] if end else None, end[1] if end else None, lines[i]))
        if layout is not None:
            spans += _running_spans(lines, layout, rx)
        for s, e, name, heading in sorted(spans, key=lambda x: x[0]):
            if e is None:
                yield fam, None, None, False
                continue
            text, toc = _section_text(lines, s, e, running, heading)
            yield fam, text, name, toc


def extract_family_section(lines: Sequence[str], kind: str, layout: Layout | None = None
                           ) -> tuple[str | None, str, tuple[str, str] | None]:
    """Business section by the bank / insurer / A+H heading families (_FAMILY_STARTS, order by kind): returns
    (text, 'ok:end=<boundary>;family=<name>', None) or (None, reason, clip) where clip = (text, note) is the first
    section that was only too long (the caller may cut it to MAX_SECTION_CHARS)."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    fams = [(f, _FAMILY_STARTS[f]) for f in _FAMILY_ORDER[kind]]
    note, clip = "start_heading_not_found", None
    for fam, text, name, toc in _family_sections(lines, fams, layout):
        if text is None:
            note = "end_heading_not_found" if note == "start_heading_not_found" else note
            continue
        if toc:
            note = "toc_only"
            continue
        if len(text) < MIN_SECTION_CHARS:
            note = f"section_too_short:{len(text)}"
            continue
        if len(text) > MAX_SECTION_CHARS:
            note = f"section_too_long:{len(text)}"
            clip = clip or (text, f"ok:end={name};family={fam};clipped:{len(text)}")
            continue
        return text, f"ok:end={name};family={fam}", None
    return None, note, clip


def extract_mda_opening(lines: Sequence[str], layout: Layout | None = None) -> tuple[str | None, str]:
    """Fallback when no business heading exists: the opening of the '管理层讨论与分析' / '经营情况讨论与分析'
    chapter (or the '经营情况概览' item), up to its end heading and at most MDA_OPENING_CHARS (cut at a paragraph
    end). Returns (text, 'ok:end=<boundary>;fallback:mda_opening') or (None, reason)."""
    note = "mda_not_found"
    for _, text, name, toc in _family_sections(lines, [("mda", _MDA_START)], layout, bare_ends=_MDA_BARE_ENDS):
        if text is None or toc:
            continue
        if len(text) > MDA_OPENING_CHARS:
            text, name = _clip(text, MDA_OPENING_CHARS), "opening_bound"
        if len(text) < MIN_SECTION_CHARS:
            note = f"mda_too_short:{len(text)}"
            continue
        return text, f"ok:end={name};fallback:mda_opening"
    return None, note


def extract_section(lines: Sequence[str], kind: str, layout: Layout | None = None) -> tuple[str | None, str]:
    """The whole cascade on cleaned lines: template rules of `kind`, then of the other kind (';rules=<other>'),
    then the bank / insurer / A+H families (';family=<name>'), then a too-long family section cut to
    MAX_SECTION_CHARS (';clipped:<chars>'), then the management discussion opening (';fallback:mda_opening').
    `layout` (page offsets + running headers, see Layout.from_pages) enables the running-header rules.
    Returns (text, note) or (None, the first specific failure reason)."""
    text, note = extract_business_section(lines, kind)
    if text is not None:
        return text, note
    other = "full" if kind == "summary" else "summary"
    text2, note2 = extract_business_section(lines, other)
    if text2 is not None:
        return text2, f"{note2};rules={other}"
    text3, note3, clip = extract_family_section(lines, kind, layout)
    if text3 is not None:
        return text3, note3
    if clip is not None:
        return _clip(clip[0], MAX_SECTION_CHARS), clip[1]
    text4, note4 = extract_mda_opening(lines, layout)
    if text4 is not None:
        return text4, note4
    for n in (note, note3):
        if n != "start_heading_not_found":
            return None, n
    return None, "start_heading_not_found"


def extract_from_pdf(data: bytes, kind: str, *, backend: str = "auto") -> tuple[str | None, str, str]:
    """PDF bytes -> (section text | None, note, backend) through extract_from_pages / extract_section (template
    rules of `kind`, the other kind's rules (note ';rules=<other>'), heading families, clipped family section,
    management discussion opening)."""
    try:
        pages, used = pdf_pages(data, backend=backend)
    except PdfError as e:
        return None, f"pdf_parse_failed:{str(e)[:120]}", "none"
    return extract_from_pages(pages, kind) + (used,)


def extract_from_pages(pages: Sequence[str], kind: str) -> tuple[str | None, str]:
    """Page texts -> (section text | None, note): clean_pages + Layout + extract_section (the text fixtures use
    this directly)."""
    offsets: list[int] = []
    lines = clean_pages(pages, offsets=offsets)
    if sum(len(ln) for ln in lines) < 50:
        return None, "no_text_layer"
    return extract_section(lines, kind, Layout.from_pages(pages, offsets))


def short_description(section_text: str | None, max_chars: int = SHORT_DESC_CHARS) -> str:
    """First ~max_chars of the section without its heading line, cut at a sentence end ('。') when possible."""
    if not section_text:
        return ""
    paras = [p.strip() for p in section_text.split("\n") if p.strip()]
    if paras and parse_heading(paras[0]):
        paras = paras[1:]
    text = "\n".join(paras)
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    pos = max(cut.rfind("。"), cut.rfind("！"), cut.rfind("？"), cut.rfind("；"))
    if pos >= max_chars * 0.4:
        return cut[:pos + 1]
    return cut.rstrip() + "…"


# =========================================================================== HTTP helpers


def post_form(client: Any, url: str, form: Mapping[str, Any], *, headers: Mapping[str, str] | None = None,
              rate_key: str | None = None) -> Response:
    """POST an application/x-www-form-urlencoded body through the polite client.

    Uses client.post_form(url, form, headers=, rate_key=) when the client has one (test fakes); an http.Client sends
    it through Client.request (same limiter / halt / Blocked / retry rules, deadline QUERY_DEADLINE_S)."""
    fn = getattr(client, "post_form", None)
    if callable(fn):
        return fn(url, dict(form), headers=dict(headers or {}), rate_key=rate_key)
    if not isinstance(client, Client):
        raise TypeError("client supports neither post_form() nor is an http.Client")
    return _client_post_form(client, url, urllib.parse.urlencode(dict(form)).encode(), dict(headers or {}), rate_key)


def _client_post_form(client: Client, url: str, data: bytes, headers: dict[str, str],
                      rate_key: str | None) -> Response:
    """Client.request with a urlencoded body: same limiter / halt / Blocked (403/429 before any body read) / retry
    rules and the same per-attempt deadline (QUERY_DEADLINE_S) and chunked body reads as every other request."""
    hdrs = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
    hdrs.update(headers)
    return client.request("POST", url, data=data, headers=hdrs, rate_key=rate_key, deadline_s=QUERY_DEADLINE_S)


def query_form(code: str, org_id: str, page_num: int = 1) -> dict[str, str]:
    return {"stock": f"{code},{org_id}", "tabName": "fulltext", "pageSize": str(QUERY_PAGE_SIZE),
            "pageNum": str(page_num), "column": QUERY_COLUMN, "category": QUERY_CATEGORY}


# =========================================================================== fast mode: bulk listing


def bulk_query_form(page_num: int, se_date: str, plate: str | None = None) -> dict[str, str]:
    """The date-range listing of all annual-report announcements (both exchanges; live probe 2026-09-26: stock=''
    with column 'szse' or 'sse' gives the same 30 rows per page, each with secCode / orgId / title / adjunctUrl).
    `plate` ('sh' / 'sz' / 'bj', live probe 2026-09-27: the three totals add up to the day's) is only sent by the day-over-cap repair (list_range)."""
    form = {"stock": "", "tabName": "fulltext", "pageSize": str(QUERY_PAGE_SIZE), "pageNum": str(page_num),
            "column": QUERY_COLUMN, "category": QUERY_CATEGORY, "seDate": se_date}
    if plate:
        form["plate"] = plate
    return form


def months_before(day: dt.date, months: int) -> dt.date:
    """`day` shifted back by whole months (the day clamped to the target month's length)."""
    y, m = divmod(day.year * 12 + day.month - 1 - int(months), 12)
    m += 1
    last = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)).day
    return dt.date(y, m, min(day.day, last))


def settle_floor(today: dt.date) -> dt.date:
    """Latest window start that still settles a company whose newest report is the one that must be out by `today`
    (expected_latest_year E): 1 January E+1. From 1 May it is 1 January of this year, before it 1 January of last
    year (the FY E+1 reports are only being published then, so most companies' newest year is still E)."""
    return dt.date(expected_latest_year(today) + 1, 1, 1)


def default_since(today: dt.date | None = None) -> dt.date:
    """LIST_WINDOW_MONTHS back from today, but never later than settle_floor(today): 13 months alone would start
    after 1 January of last year from early February to 30 April, and leave every company that has not yet
    published its new report unsettled."""
    day = today or dt.datetime.now(CN_TZ).date()
    return min(months_before(day, LIST_WINDOW_MONTHS), settle_floor(day))


def parse_since(value: Any) -> dt.date | None:
    """None | date | 'YYYY-MM-DD' -> date (ValueError for anything else)."""
    if value is None or isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value).strip())


def _ann_key(a: Mapping[str, Any]) -> str:
    return str(a.get("announcementId") or a.get("adjunctUrl") or a.get("announcementTitle") or "")


class Listing:
    """Result of one bulk listing pass (filled page by page, so a pass cut short by a block is still reported):
    announcements (de-duplicated by announcementId), pages fetched, the raw manifest (one entry per page), the stop
    reason and whether the pass is complete (stopped by the server's own end signals)."""

    def __init__(self, since: dt.date, until: dt.date, *, plate: str | None = None, role: str = "window") -> None:
        self.since, self.until = since, until
        self.se_date = f"{since.isoformat()}~{until.isoformat()}"
        self.plate = plate                      # 'plate' filter of the day-over-cap repair (None: all markets)
        self.role = role                        # 'window' (list_range's tiling) | 'retry' | 'plate:<p>' (repairs)
        #                                         | 'empty_probe' (a whole-range page 1 without rows, asked again)
        self.repair: str | None = None          # how an incomplete window was repaired ('retry' / 'plate')
        self.announcements: list[dict] = []
        self.pages = 0
        self.manifest: list[dict] = []
        self.stop: str | None = None
        self.complete = False
        self.error: str | None = None
        self.total_reported: int | None = None
        self.total_pages: int | None = None     # the server's 'totalpages' (floor(total / 30): NOT the page count)
        self.total_first: int | None = None     # totalAnnouncement of page 1: the rows the pass must see
        self.shortfall: int | None = None       # total_first - unique rows seen, when the pass saw fewer
        self.duration_s = 0.0
        self.bytes = 0
        self.started_at: dt.datetime | None = None

    def expected_pages(self) -> int | None:
        """Real page count: ceil(totalAnnouncement of page 1 / 30); the server's totalpages is floor(total / 30)
        (live: total 67 -> totalpages 2, 3 real pages), used only when no total was given."""
        if self.total_first is not None:
            return math.ceil(self.total_first / QUERY_PAGE_SIZE)
        return self.total_pages

    def groups(self) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
        """(orgId -> rows, secCode -> rows)."""
        return group_rows(self.announcements)


def group_rows(announcements: Iterable[Mapping[str, Any]]) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """Listing rows -> (orgId -> rows, secCode -> rows)."""
    by_org: dict[str, list[dict]] = {}
    by_code: dict[str, list[dict]] = {}
    for a in announcements:
        org, code = str(a.get("orgId") or "").strip(), str(a.get("secCode") or "").strip()
        if org:
            by_org.setdefault(org, []).append(a)
        if code:
            by_code.setdefault(code, []).append(a)
    return by_org, by_code


def list_window(client: Any, cfg: Config, listing: Listing, *, max_pages: int = MAX_LIST_PAGES,
                split_above: int | None = None, log: Callable[[str], None] | None = None) -> Listing:
    """Page through the date-range query of ONE window on rate_key 'cninfo' (the client's limiter keeps >= 1.0 s
    between starts).

    Each page is saved raw (store.save_raw 'list-p<NNNN>-<since>-<until>'). `split_above`: when page 1 reports more
    rows than this, stop at once with stop='split' (the caller splits the window; page 1's rows are not kept).
    Stops on: hasMore == false, a page with fewer than QUERY_PAGE_SIZE rows, an empty page or a page whose rows were
    all seen already (incomplete when the expected page count, ceil(totalAnnouncement of page 1 / 30), says more
    pages exist), CNINFO's page cap PAGE_CAP_PAGES ('page_cap': page PAGE_CAP_PAGES + 1 is never requested; live, it
    repeats page 1), max_pages / expected pages + 3 ('page_limit', incomplete), an HTTP error or bad JSON
    (incomplete, listing.error). Rows seen on an earlier page (the listing shifted while paging) are dropped. A pass
    that ends normally is complete only when it saw at least as many distinct rows as page 1 reported (new
    announcements while paging only add rows at the top, so fewer means rows were lost at a page boundary:
    announcementTime has day granularity, so the order of rows published the same day is not stable); otherwise
    listing.shortfall is set and the pass is incomplete.
    Blocked / Halted / KeyboardInterrupt propagate (the pages so far stay in `listing`)."""
    seen: set[str] = set()
    listing.started_at = listing.started_at or store.now_utc()
    tag = f"{listing.since:%Y%m%d}-{listing.until:%Y%m%d}" + (f"-{listing.plate}" if listing.plate else "")
    page = 0
    while True:
        page += 1
        expected = listing.expected_pages()
        limit = max_pages if expected is None else min(max_pages, expected + 3)
        if page > limit:
            listing.stop, listing.complete = "page_limit", False
            break
        if page > PAGE_CAP_PAGES:
            # CNINFO never serves more pages (page 101 is page 1 again): complete only if nothing is missing
            listing.stop = "page_cap"
            listing.complete = listing.total_first is not None and len(listing.announcements) >= listing.total_first
            break
        t = time.monotonic()
        form = bulk_query_form(page, listing.se_date, listing.plate)
        try:
            resp = post_form(client, QUERY_URL, form, headers=QUERY_HEADERS, rate_key=RATE_KEY)
        finally:
            listing.duration_s += time.monotonic() - t
        body = resp.body or b""
        listing.bytes += len(body)
        if resp.status != 200:
            listing.stop, listing.error, listing.complete = "error", f"list_http_{resp.status}", False
            break
        try:
            data = json.loads(body)
            if not isinstance(data, Mapping):
                raise ValueError("not an object")
        except ValueError:
            listing.stop, listing.error, listing.complete = "error", "list_bad_json", False
            break
        listing.pages = page
        rows = [a for a in (data.get("announcements") or []) if isinstance(a, Mapping)]
        path, dig = store.save_raw(cfg, SOURCE_ID, f"list-p{page:04d}-{tag}", body)
        listing.manifest.append({"page": page, "window": listing.se_date, "raw_path": path, "raw_sha256": dig,
                                 "raw_bytes": len(body), "rows": len(rows), "fetched_at": store.now_utc().isoformat(),
                                 **({"plate": listing.plate} if listing.plate else {})})
        for k, attr in (("totalAnnouncement", "total_reported"), ("totalpages", "total_pages")):
            try:
                v = int(data.get(k)) if data.get(k) is not None else None
            except (TypeError, ValueError):
                v = None
            if v is not None and (v > 0 or attr == "total_reported"):   # totalpages 0 = not given
                setattr(listing, attr, v)
        if page == 1 and listing.total_reported is not None:
            listing.total_first = listing.total_reported
            if split_above is not None and listing.total_first > split_above:
                listing.stop, listing.complete = "split", False
                break
        keys = [_ann_key(a) for a in rows]
        fresh = [a for a, k in zip(rows, keys) if k not in seen]
        expected = listing.expected_pages()
        more_expected = bool(expected) and page < (expected or 0)
        if not rows or not fresh:
            listing.stop = "empty_page" if not rows else "duplicate_page"
            listing.complete = not more_expected
            break
        for a, k in zip(rows, keys):
            if k not in seen:
                seen.add(k)
                listing.announcements.append(dict(a))
        if log is not None and page % 50 == 0:
            log(f"listing {listing.se_date} page {page} ({len(listing.announcements)} announcements"
                f"{'' if listing.total_reported is None else f' of {listing.total_reported}'})")
        if data.get("hasMore") is False:
            listing.stop, listing.complete = "has_more_false", True
            break
        if len(rows) < QUERY_PAGE_SIZE:
            listing.stop, listing.complete = "short_page", True
            break
    if listing.complete and listing.total_first is not None and len(listing.announcements) < listing.total_first:
        listing.shortfall = listing.total_first - len(listing.announcements)
        listing.complete = False
        listing.error = f"rows_missing:{len(listing.announcements)}_of_{listing.total_first}"
    return listing


class RangeListing:
    """One bulk listing pass over [since, until] made of date windows (list_range). Same reporting surface as a
    Listing (announcements, pages, manifest, stop, complete, error, se_date, duration_s, bytes, started_at, groups)
    plus: windows (every request series: split probes, leaf windows, repair listings and a whole-range probe that
    answered no rows, role 'empty_probe'), gaps [(start, end,
    reason)] = leaf windows that stayed incomplete after their repair (a single day over CNINFO's 3,000-row cap:
    reason 'day_over_cap'), days_over_cap (ISO dates, repaired or not), repairs ['<se_date>:retry|plate'],
    split_mismatches [(start, end, parent_total, children_total)], usable (the pass ran to the end and the split
    totals add up: rows outside the gaps are complete) and complete (usable and no gap)."""

    def __init__(self, since: dt.date, until: dt.date) -> None:
        self.since, self.until = since, until
        self.se_date = f"{since.isoformat()}~{until.isoformat()}"
        self.windows: list[Listing] = []
        self.announcements: list[dict] = []
        self.pages = 0
        self.stop: str | None = None
        self.complete = False
        self.usable = False
        self.error: str | None = None
        self.gaps: list[tuple[dt.date, dt.date, str]] = []
        self.days_over_cap: list[str] = []
        self.repairs: list[str] = []
        self.split_mismatches: list[tuple[dt.date, dt.date, int, int]] = []
        self.total_first: int | None = None     # page 1 of the whole range (the first probe)
        self.total_reported: int | None = None  # sum of the leaf windows' first-pass page-1 totals
        self.window_rows = 0                    # sum of the leaf windows' distinct rows
        self.shortfall: int | None = None       # total_reported - window_rows when positive
        self.total_pages: int | None = None
        self.duration_s = 0.0
        self.bytes = 0
        self.started_at: dt.datetime | None = None
        self._seen: set[str] = set()

    @property
    def leaves(self) -> list[Listing]:
        return [w for w in self.windows if w.stop != "split" and w.role == "window"]

    @property
    def splits(self) -> int:
        return sum(1 for w in self.windows if w.stop == "split" and w.role == "window")

    @property
    def manifest(self) -> list[dict]:
        return [m for w in self.windows for m in w.manifest]

    def windows_manifest(self) -> list[dict]:
        return [{"se_date": w.se_date, "pages": w.pages, "total": w.total_first, "rows": len(w.announcements),
                 "stop": w.stop, "complete": w.complete, "error": w.error, "role": w.role,
                 **({"plate": w.plate} if w.plate else {}), **({"repair": w.repair} if w.repair else {})}
                for w in self.windows]

    def add(self, window: Listing) -> None:
        for a in window.announcements:
            k = _ann_key(a)
            if k not in self._seen:
                self._seen.add(k)
                self.announcements.append(a)
        self.window_rows += len(window.announcements)
        if window.total_first is not None:
            self.total_reported = (self.total_reported or 0) + window.total_first

    def groups(self) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
        return group_rows(self.announcements)

    def gap_affects(self, announcements: Iterable[Mapping[str, Any]] | None) -> bool:
        """True when a gap could hold a row that changes the company's selection: a report of its newest listed
        year Y (or a newer one) can only be published from 1 January Y+1 on, so a gap ending before that day cannot
        matter; a company without any annual-report-like row in the listing is affected by every gap. (Dates only:
        a gap inside the reporting season affects nearly every company - list_range repairs gaps first.)"""
        if not self.gaps:
            return False
        cands, unparsed = annual_report_scan(announcements)
        years = [c["year"] for c in cands] + [u["year"] for u in unparsed]
        if not years:
            return True
        first_day = dt.date(max(years) + 1, 1, 1)
        return any(end >= first_day for _, end, _ in self.gaps)


def totals_agree(parent: int, children: int) -> bool:
    """Split check: the children of a date window must report as many rows as the window's own page 1 - at most
    SPLIT_SLACK_DOWN fewer (an announcement withdrawn meanwhile) and at most max(SPLIT_SLACK_UP_MIN, 1%) more (rows
    published while the pass runs). Fewer means rows are lost at the split boundary (seDate's end exclusive, or not
    at China-date granularity), more means the children overlap."""
    return parent - SPLIT_SLACK_DOWN <= children <= parent + max(SPLIT_SLACK_UP_MIN, parent // 100)


def _merge_rows(w: Listing, rows: Iterable[Mapping[str, Any]]) -> None:
    seen = {_ann_key(a) for a in w.announcements}
    for a in rows:
        k = _ann_key(a)
        if k not in seen:
            seen.add(k)
            w.announcements.append(dict(a))


def list_range(client: Any, cfg: Config, rl: RangeListing, *, max_rows: int | None = None,
               max_pages: int = MAX_LIST_PAGES, log: Callable[[str], None] | None = None) -> RangeListing:
    """The bulk listing of [rl.since, rl.until] in adaptive date windows (CNINFO serves at most PAGE_CAP_PAGES
    pages = 3,000 rows per query; live 2026-09-26 a 13-month query reported 11,403 rows and page 101 repeated
    page 1).

    Each window is read with list_window: when its page 1 reports more than `max_rows` (WINDOW_MAX_ROWS, a margin
    below the cap for rows added while paging) and it spans more than one day, it is split in half by date (newer
    half first) and page 1 is thrown away (one probe request); otherwise it is paged to its end. A single day is
    always paged: above the cap it stops at page PAGE_CAP_PAGES ('day_over_cap:<date>').

    Repairs (a gap in the reporting season affects nearly every company, see RangeListing.gap_affects, so it is
    repaired before it is recorded): a day over the cap is re-listed per exchange (LIST_PLATES, the 'plate' filter;
    accepted only when every plate stays under the cap, holds only its own market's codes, is complete and the
    plate totals add up to at least the day's total (no downward slack; at most the split check's upward slack
    more) - otherwise the day stays a gap; a server that ignores the filter costs one request); any other
    incomplete leaf (rows missing, early empty / duplicate page) is paged once more and the two passes' rows are
    united (complete when the union reaches the retry's page-1 total; a retry reporting fewer rows than the first
    pass knew of - a blank / throttled reply, a withdrawal - is a failed repair: 'retry_total_dropped'). A leaf
    still incomplete becomes a gap [start, end]: the pass stays usable, only companies whose newest-year rows could
    fall inside it need their own query.

    Split check (seDate 'a~b' is taken as inclusive at China-date granularity): once both halves of a split window
    are read, the halves' page-1 totals must agree with the window's (totals_agree), and at the end the leaves'
    totals with the whole-range probe; otherwise the pass is unusable ('split_totals:...'). A leaf enters these
    checks with its FIRST pass's page-1 total (read right after its parent's probe; a repair's totals are newer by
    a whole pass and only decide the leaf's own completeness). A whole-range page 1 without rows is asked once
    more; twice without rows the pass is unusable ('empty_range': a blank reply cannot be told from an empty
    range). An HTTP error, bad JSON, a network error (propagated) or the max_pages guard over all windows make the
    pass unusable too. Completeness: sum over leaf windows of their distinct rows vs the sum of their page-1 totals
    (rl.shortfall). Blocked / Halted / KeyboardInterrupt propagate (windows so far stay in `rl`)."""
    max_rows = WINDOW_MAX_ROWS if max_rows is None else int(max_rows)
    rl.started_at = rl.started_at or store.now_utc()

    def run(w: Listing, split_above: int | None) -> Listing:
        rl.windows.append(w)
        try:
            list_window(client, cfg, w, max_pages=max(0, max_pages - rl.pages), split_above=split_above, log=log)
        finally:
            rl.pages += w.pages
            rl.duration_s += w.duration_s
            rl.bytes += w.bytes
        return w

    def repair_by_plate(w: Listing) -> str | None:
        """Re-list the single day `w` per exchange; None when it is complete now, else why not."""
        day_total = w.total_first or 0
        parts: list[Listing] = []
        for plate, market in LIST_PLATES:
            if rl.pages >= max_pages:
                return "plate_budget"
            p = run(Listing(w.since, w.until, plate=plate, role=f"plate:{plate}"),
                    PAGE_CAP_PAGES * QUERY_PAGE_SIZE)
            if p.stop == "split":        # still over the cap: the filter was ignored, or one exchange alone is over
                return f"plate_ignored:{plate}" if (p.total_first or 0) >= day_total - SPLIT_SLACK_DOWN \
                    else f"plate_over_cap:{plate}"
            if p.stop == "error":
                return f"plate_error:{plate}:{p.error}"
            if any(market_of_code(str(a.get("secCode") or "")) not in (None, market) for a in p.announcements):
                return f"plate_mixed:{plate}"
            if not p.complete or p.total_first is None:
                return f"plate_incomplete:{plate}:{p.error or p.stop}"
            parts.append(p)
        plate_total = sum(p.total_first or 0 for p in parts)
        # no downward slack: rows of the day that no honoured plate returns (an unknown plate value, a plate that
        # leaves some codes out) would be lost silently beyond the first pass's cap; rows of a past day are not
        # withdrawn within seconds, and a short plate total only makes the day a gap (safe)
        if not day_total <= plate_total <= day_total + max(SPLIT_SLACK_UP_MIN, day_total // 100):
            return f"plate_totals:{plate_total}_vs_{day_total}"
        for p in parts:
            _merge_rows(w, p.announcements)
        # w.total_first stays the first pass's total (the split checks compare totals of the same moment)
        target = max(plate_total, day_total)
        if len(w.announcements) < target:
            return f"plate_rows_missing:{len(w.announcements)}_of_{target}"
        return None

    def repair_by_retry(w: Listing) -> str | None:
        """Page the window once more and unite both passes' rows; None when complete now."""
        if rl.pages >= max_pages:
            return "retry_budget"
        floor = max(w.total_first or 0, len(w.announcements))       # what the first pass already knew
        r = run(Listing(w.since, w.until, role="retry"), None)
        if r.stop == "error" or (r.stop == "page_limit" and rl.pages >= max_pages):
            return "retry_failed"
        if r.total_first is not None and r.total_first < floor:
            # rows are only added over time: a lower total is a blank / throttled reply or a withdrawal, and
            # accepting it could mark rows the first pass knew of as not missing
            return f"retry_total_dropped:{r.total_first}_vs_{floor}"
        _merge_rows(w, r.announcements)
        # the retry's total is newer than the parent's probe by the whole first pass: it decides the window's own
        # completeness only; w.total_first (split and whole-range checks) stays the first pass's total
        target = w.total_first if r.total_first is None else r.total_first
        if w.total_first is None:
            w.total_first = r.total_first
        if target is not None and len(w.announcements) >= target:
            return None
        return f"rows_missing:{len(w.announcements)}_of_{target}"

    # split tree: node id -> [start, end, own page-1 total, children still open, children's totals, parent id]
    nodes: dict[int, list] = {}
    node_ids = itertools.count()

    def settle(parent: int | None, total: int | None) -> None:
        """A child of `parent` finished with page-1 total `total`: check the parent once both children are in."""
        while parent is not None:
            node = nodes[parent]
            node[3] -= 1
            node[4] = None if (total is None or node[4] is None) else node[4] + total
            if node[3] > 0:
                return
            start, end, own, _, children, grand = node
            if own is not None and children is not None and not totals_agree(own, children):
                rl.split_mismatches.append((start, end, own, children))
                if log is not None:
                    log(f"WARNING: listing window {start}~{end} reported {own} rows but its halves {children}")
            parent, total = grand, own

    stack: list[tuple[dt.date, dt.date, int | None]] = [(rl.since, rl.until, None)]
    while stack:
        start, end, parent = stack.pop()
        if max_pages - rl.pages <= 0:
            rl.stop, rl.error = "page_limit", f"list_pages_over_{max_pages}"
            break
        w = run(Listing(start, end), max_rows if start < end else None)
        if parent is None and w.stop == "empty_page" and not w.announcements:
            # the whole range answers no rows: a blank / throttled reply looks exactly like this (total 0, and
            # nothing else checks a single-window pass), so ask once more; twice nothing is not trusted either
            w.role = "empty_probe"
            if log is not None:
                log(f"listing {w.se_date}: page 1 answered no rows; asking once more")
            w = run(Listing(start, end), max_rows if start < end else None)
            if w.stop == "empty_page" and not w.announcements:
                rl.total_first = w.total_first
                rl.stop, rl.error = "empty_range", f"empty_range:{w.se_date}"
                rl.add(w)
                if log is not None:
                    log(f"WARNING: listing {w.se_date} answered no rows twice: listing not used")
                break
        if rl.total_first is None:
            rl.total_first = w.total_first
        if w.stop == "split":
            mid = start + dt.timedelta(days=(end - start).days // 2)
            nid = next(node_ids)
            nodes[nid] = [start, end, w.total_first, 2, 0, parent]
            stack.append((start, mid, nid))
            stack.append((mid + dt.timedelta(days=1), end, nid))    # popped first: newest rows first, as CNINFO
            if log is not None:
                log(f"listing window {w.se_date}: {w.total_first} rows > {max_rows}, split at {mid}")
            continue
        if w.stop == "error" or (w.stop == "page_limit" and rl.pages >= max_pages):
            rl.stop, rl.error = w.stop, w.error or f"list_pages_over_{max_pages}"
            break
        if not w.complete:
            over = start == end and (w.total_first or 0) > PAGE_CAP_PAGES * QUERY_PAGE_SIZE
            if over:
                rl.days_over_cap.append(start.isoformat())
            first = "day_over_cap" if over else (w.error or w.stop or "incomplete")
            failed = repair_by_plate(w) if over else repair_by_retry(w)
            if failed is None:
                w.repair = "plate" if over else "retry"
                w.complete, w.error, w.shortfall = True, None, None
                rl.repairs.append(f"{w.se_date}:{w.repair}")
                if log is not None:
                    log(f"listing window {w.se_date} was incomplete ({first}); repaired by "
                        f"{'the per-exchange listing' if over else 'a second pass'}: {len(w.announcements)} rows")
            else:
                reason = failed if not over and failed.startswith("rows_missing") else first
                if reason is failed:
                    w.error = failed
                rl.gaps.append((start, end, reason))
                if log is not None:
                    log(f"WARNING: listing window {w.se_date} incomplete ({first}; repair: {failed}: "
                        f"{len(w.announcements)} of {w.total_first} rows); companies whose newest report could fall "
                        f"in it get their own query")
        elif log is not None:
            log(f"listing window {w.se_date}: {len(w.announcements)} rows in {w.pages} pages (stop={w.stop}); "
                f"{rl.pages} listing requests so far")
        rl.add(w)
        settle(parent, w.total_first)
    else:
        rl.stop, rl.usable = "done", True
        if len(rl.leaves) > 1 and rl.total_first is not None and rl.total_reported is not None \
                and not totals_agree(rl.total_first, rl.total_reported) \
                and not any(a == rl.since and b == rl.until for a, b, _, _ in rl.split_mismatches):
            rl.split_mismatches.append((rl.since, rl.until, rl.total_first, rl.total_reported))
        if rl.split_mismatches:
            a, b, own, children = rl.split_mismatches[0]
            rl.stop, rl.usable = "split_totals", False
            rl.error = f"split_totals:{a}~{b}:{children}_vs_{own}"
            if log is not None:
                log(f"WARNING: the split windows' totals do not add up ({len(rl.split_mismatches)} mismatch(es), "
                    f"first {rl.error}): seDate may not be inclusive by China date; listing not used")
        rl.complete = rl.usable and not rl.gaps
    if rl.total_reported is not None and rl.total_reported > rl.window_rows:
        rl.shortfall = rl.total_reported - rl.window_rows
    return rl


def company_announcements(by_org: Mapping[str, list[dict]], by_code: Mapping[str, list[dict]], org: str,
                          codes: Iterable[str]) -> list[dict]:
    """A company's listing rows (by orgId, plus any row of one of its codes), de-duplicated, newest first."""
    out, seen = [], set()
    for a in itertools.chain(by_org.get(org) or [], *[by_code.get(c) or [] for c in codes]):
        k = _ann_key(a)
        if k not in seen:
            seen.add(k)
            out.append(a)
    out.sort(key=lambda a: int(a.get("announcementTime") or 0) if str(a.get("announcementTime") or "0").isdigit()
             else 0, reverse=True)
    return out


def window_settles(announcements: Iterable[Mapping[str, Any]] | None, since: dt.date) -> bool:
    """True when the listing rows of one company give the same selection as its per-company query: the newest
    year Y among its annual-report-like rows (parsed or not) has every report inside the window, i.e. the window
    starts no later than 1 January Y+1 (a fiscal year's reports are published after its end). Older years outside
    the window never change the choice of the newest year, and the newest-year guard only looks at newer years."""
    cands, unparsed = annual_report_scan(announcements)
    years = [c["year"] for c in cands] + [u["year"] for u in unparsed]
    if not years:
        return False
    return since <= dt.date(max(years) + 1, 1, 1)


def clamp_workers(workers: Any) -> int:
    """PDF workers: default DEFAULT_WORKERS, clamped to 1..MAX_WORKERS."""
    if workers is None:
        return DEFAULT_WORKERS
    try:
        n = int(workers)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_WORKERS
    return max(1, min(n, MAX_WORKERS))


def static_client_for(client: Any) -> Any:
    """The PDF client of fast mode: for an http.Client, a second http.Client on the same opener, User-Agent and
    timeouts, with STATIC_MIN_INTERVAL_S / STATIC_MAX_PER_WINDOW, sharing the SAME halt Event (a block seen on either
    key stops both). Any other client (test fakes) is used as is."""
    if not isinstance(client, Client):
        return client
    return Client(user_agent=client.user_agent, min_interval_s=STATIC_MIN_INTERVAL_S, timeout_s=client.timeout_s,
                  max_retries=client.max_retries, max_bytes=client.max_bytes, _opener=client._opener,
                  halt=client.halt, max_per_window=STATIC_MAX_PER_WINDOW, window_s=1.0,
                  deadline_s=client.deadline_s)


# =========================================================================== sync

_DOC_COLS = ["doc_id", "security_id", "company_key", "source_id", "cik", "form", "section", "accession",
             "filing_date", "report_date", "url", "raw_sha256", "raw_bytes", "text_path", "text_sha256", "text_chars",
             "extractor", "extract_note", "fetched_at", "snapshot_id"]
_DESC_COLS = ["security_id", "source_id", "company_key", "text", "text_sha256", "lang", "source_url", "fetched_at",
              "match_method", "match_score", "snapshot_id"]
_STATE_COLS = ["source_id", "security_id", "status", "http_status", "attempts", "last_attempt_at", "note"]


def text_path_for(cfg: Config, code: str, announcement_id: str) -> Path:
    return Path(cfg.home) / "docs" / "cninfo" / str(code) / f"{announcement_id}-{SECTION}.txt"


def doc_id_for(org_id: str, announcement_id: str) -> str:
    return f"{SOURCE_ID}:{org_id}:{announcement_id}:{SECTION}"


SKIP_CURRENT = object()    # fast-mode plan marker: the stored report is already the newest possible year


class _Batch:
    def __init__(self) -> None:
        self.queries: list[dict] = []
        self.docs: list[dict] = []
        self.descs: list[dict] = []
        self.states: list[list[Any]] = []
        self.started_at: dt.datetime | None = None
        self.duration_s = 0.0
        self.groups = 0
        self.listing: Listing | RangeListing | None = None   # fast mode: the listing pass, snapshotted at a flush
        self.pdf_rate_key = RATE_KEY

    def __len__(self) -> int:
        return len(self.states) + len(self.queries) + len(self.docs) + (self.listing is not None)


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v


def _flush(cfg: Config, batch: _Batch, attempts: dict[str, int], note: str | None = None, *,
           wait_s: float = FINAL_WAIT_S) -> int:
    """One short write session per batch: snapshots + documents + descriptions + crawl_state, atomically."""
    if not len(batch):
        return 0
    snaps = 0
    q_manifest = json.dumps(batch.queries, sort_keys=True, ensure_ascii=False).encode() if batch.queries else None
    d_manifest = (json.dumps([{k: _iso(v) for k, v in d.items()} for d in batch.docs], sort_keys=True,
                             ensure_ascii=False).encode() if batch.docs else None)
    lst = batch.listing
    l_extra = ({"windows": lst.windows_manifest(), "gaps": [[a.isoformat(), b.isoformat(), r] for a, b, r in lst.gaps],
                "days_over_cap": lst.days_over_cap, "usable": lst.usable, "window_rows": lst.window_rows,
                "rows": len(lst.announcements), "total_first": lst.total_first, "repairs": lst.repairs,
                "split_mismatches": [[a.isoformat(), b.isoformat(), o, c] for a, b, o, c in lst.split_mismatches]}
               if isinstance(lst, RangeListing) else {})
    l_manifest = (json.dumps({"se_date": lst.se_date, "pages": lst.manifest, "stop": lst.stop,
                              "complete": lst.complete, "error": lst.error, "total_reported": lst.total_reported,
                              "total_pages": lst.total_pages, **l_extra}, sort_keys=True,
                             ensure_ascii=False).encode()
                  if lst is not None else None)
    with store.session(cfg, wait_s=wait_s) as con:
        q_path = q_dig = d_path = d_dig = l_path = l_dig = None
        if q_manifest is not None:
            q_path, q_dig = store.save_raw(cfg, SOURCE_ID, "query_batch", q_manifest)
        if d_manifest is not None:
            d_path, d_dig = store.save_raw(cfg, SOURCE_ID, "report_batch", d_manifest)
        if l_manifest is not None:
            l_path, l_dig = store.save_raw(cfg, SOURCE_ID, "list_pass", l_manifest)
        con.begin()
        try:
            if lst is not None:
                form = {k: v for k, v in bulk_query_form(1, lst.se_date).items() if k != "pageNum"}
                store.record_snapshot(
                    con, source_id=SOURCE_ID, kind="list_pass",
                    request={"method": "POST", "url": QUERY_URL, "rate_key": RATE_KEY, "form": form,
                             "pages": lst.pages},
                    raw_path=l_path, raw_sha256=l_dig, raw_bytes=len(l_manifest), rows=len(lst.announcements),
                    duration_s=round(lst.duration_s, 3), status="ok" if lst.complete else "partial",
                    note=_listing_note(lst), fetched_at=lst.started_at)
                snaps += 1
            if batch.queries:
                store.record_snapshot(
                    con, source_id=SOURCE_ID, kind="query_batch",
                    request={"method": "POST", "url": QUERY_URL, "rate_key": RATE_KEY,
                             "forms": [q["form"] for q in batch.queries]},
                    raw_path=q_path, raw_sha256=q_dig, raw_bytes=len(q_manifest), rows=len(batch.queries),
                    duration_s=round(batch.duration_s, 3), note=note, fetched_at=batch.started_at)
                snaps += 1
            snap_id = None
            if batch.docs:
                snap_id = store.record_snapshot(
                    con, source_id=SOURCE_ID, kind="report_batch",
                    request={"method": "GET", "rate_key": batch.pdf_rate_key, "urls": [d["url"] for d in batch.docs]},
                    raw_path=d_path, raw_sha256=d_dig, raw_bytes=len(d_manifest), rows=len(batch.docs),
                    duration_s=round(batch.duration_s, 3), note=note, fetched_at=batch.started_at)
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


def _listing_note(lst: Any) -> str:
    """list_pass snapshot note: 'stop=<stop>[;error=<error>]', for a windowed pass also ';windows=<leaves>'
    (when more than one), ';pages=<requests>;rows=<distinct rows>', ';day_over_cap:<date>' per day over CNINFO's
    cap, ';repaired:<se_date>:<how>' per repaired window and ';gap:<start>~<end>:<reason>' per other incomplete
    window."""
    if isinstance(lst, RangeListing):
        leaves = lst.leaves
        stop = lst.stop
        if len(leaves) == 1 and lst.usable:
            stop = leaves[0].stop            # one window: the old, per-window stop reason
        note = f"stop={stop}" + (f";error={lst.error}" if lst.error else "")
        if len(leaves) > 1:
            note += f";windows={len(leaves)}"
        note += f";pages={lst.pages};rows={len(lst.announcements)}"
        note += "".join(f";day_over_cap:{d}" for d in lst.days_over_cap)
        note += "".join(f";repaired:{r}" for r in lst.repairs)
        note += "".join(f";gap:{a}~{b}:{r}" for a, b, r in lst.gaps if r != "day_over_cap")
        if len(leaves) == 1 and lst.gaps and leaves[0].error and not lst.error:
            note += f";error={leaves[0].error}"
        return note
    return f"stop={lst.stop}" + (f";error={lst.error}" if lst.error else "")


def _load_lines(con, only_universe: bool) -> list[dict]:
    cols = ["security_id", "exchange", "symbol", "company_key", "market_cap_usd", "isin"]
    where = "upper(exchange) IN ('SSE', 'SZSE', 'BJSE', 'BSE')"
    if only_universe:
        sql = f"SELECT {', '.join(cols)} FROM universe WHERE {where}"
    else:
        sql = (f"SELECT s.security_id, s.exchange, s.symbol, s.company_key, lm.market_cap_usd, s.isin "
               f"FROM securities s LEFT JOIN latest_market lm USING (security_id) WHERE s.active AND "
               f"upper(s.exchange) IN ('SSE', 'SZSE', 'BJSE', 'BSE')")
    rows = [dict(zip(cols, r)) for r in con.execute(sql).fetchall()]
    return [r for r in rows if is_cn_line(r)]


def _err_note(prefix: str, e: BaseException, limit: int = 200) -> str:
    return f"{prefix}{type(e).__name__}: {e}"[:len(prefix) + limit]


def _norm_codes(codes: Iterable[str] | None) -> set[str] | None:
    if codes is None:
        return None
    if isinstance(codes, str):
        codes = [c for c in re.split(r"[,\s]+", codes) if c]
    return {str(c).strip().upper().rsplit(":", 1)[-1] for c in codes}


def sync(cfg: Config, client: Any = None, *, limit: int | None = None, codes: Iterable[str] | None = None,
         kind: str = "summary", refresh: bool = False, min_mcap_usd: float | None = None, batch_size: int = 20,
         only_universe: bool = True, progress_every: int = 50,
         on_blocked: Callable[[BaseException], Any] | None = None, per_company: bool = False,
         workers: int | None = DEFAULT_WORKERS, since: Any = None, max_fallback: int | None = MAX_FALLBACK_QUERIES,
         pdf_client: Any = None, list_min_queue: int | None = None,
         on_company: Callable[[list[str], str, str | None], None] | None = None) -> dict:
    """Map mainland universe lines to CNINFO orgIds and pull the latest annual report's business section.

    Steps: (1) GET the stock list, save raw, snapshot, upsert identifiers ('cninfo_orgid') and drop stale ones of
    considered lines; (2) queue distinct orgIds by market cap desc (min_mcap_usd, codes and limit filter the queue);
    (3) fast mode (default): one bulk listing pass over [since, today] in adaptive date windows under CNINFO's
    100-page cap (list_range; since defaults to 13 months ago), each company's rows go through the same
    select_annual_reports; companies already holding a report of the newest possible fiscal year are skipped
    before any query ('skipped_current', unless refresh); companies the window does not settle get the per-company
    query (at most max_fallback, None = no cap; companies a gap of the listing could affect: no cap); a queue of at
    most list_min_queue companies (default LIST_MIN_QUEUE) or a `codes` filter skips the listing and queries each
    company; PDFs are fetched + extracted by `workers` threads
    on rate_key 'cninfo-static' (pdf_client, default static_client_for(client)). per_company=True: per orgId POST
    the announcement query, then the PDFs, all on rate_key 'cninfo', sequential. Either way a report already
    processed is skipped before any PDF request (unless refresh) and per batch documents / descriptions /
    crawl_state are upserted in one short session. Returns a summary dict whose 'status' is
    ok | blocked | stopped_errors | interrupted | store_locked.
    `client`: http.Client-compatible (get(url, headers=, rate_key=, max_bytes=) and post_form or an http.Client);
    None builds one from cfg. Its min_interval_s is raised to DEFAULT_MIN_INTERVAL_S if lower.
    on_company(security_ids, status, note): called on the main thread for every company whose status is recorded,
    and with 'skipped_current' / 'skipped_unchanged' / 'skipped_known_failed' for a company settled without a new
    download (the on-demand fetch's per-company events); its errors are ignored."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, not {kind!r}")
    since_d = parse_since(since)
    if client is None:
        client = Client(user_agent=cfg.user_agent, min_interval_s=max(DEFAULT_MIN_INTERVAL_S, cfg.min_interval_s),
                        timeout_s=cfg.timeout_s)
    cur = getattr(client, "min_interval_s", None)
    if isinstance(cur, (int, float)) and cur < DEFAULT_MIN_INTERVAL_S:
        client.min_interval_s = DEFAULT_MIN_INTERVAL_S
    if not per_company and pdf_client is None:
        pdf_client = static_client_for(client)
    budget = guard.RATE_BUDGETS.get(COMMAND, RATE_KEY)
    with guard.budget_lock(cfg, budget, reentrant=True):
        return _sync(cfg, client, limit=limit, codes=_norm_codes(codes), kind=kind, refresh=refresh,
                     min_mcap_usd=min_mcap_usd, batch_size=max(1, int(batch_size)), only_universe=only_universe,
                     progress_every=progress_every, on_blocked=on_blocked, per_company=bool(per_company),
                     workers=1 if per_company else clamp_workers(workers), since=since_d,
                     max_fallback=None if max_fallback is None else max(0, int(max_fallback)),
                     pdf_client=client if per_company else pdf_client, list_min_queue=list_min_queue,
                     on_company=on_company)


def non_pdf_body(resp: Any) -> str | None:
    """None when a 200 PDF response looks like a PDF ('%PDF' within the first 1,024 bytes, as the PDF spec allows),
    else why not: 'page' for an HTML / JSON / text page (Content-Type, or a body starting with '<' or '{'): a WAF /
    rate-limit / error page served as 200, a soft block; 'bytes' for anything else (empty or unknown bytes)."""
    body = getattr(resp, "body", b"") or b""
    if b"%PDF" in body[:1024]:
        return None
    headers = getattr(resp, "headers", None) or {}
    ctype = next((str(v) for k, v in headers.items() if str(k).lower() == "content-type"), "").lower()
    head = body[:1024].lstrip()
    if any(t in ctype for t in ("html", "json", "text/", "xml")) or head[:1] in (b"<", b"{"):
        return "page"
    return "bytes"


class _Outcome:
    """What one company's report task produced. Built without the database or any shared counter (safe in a worker
    thread); the main thread applies it in completion order. exc: Blocked / Halted (nothing of the company is
    recorded, as before) or a network error on a PDF (recorded 'error', rows of earlier candidates kept)."""

    def __init__(self) -> None:
        self.status: str | None = None       # ok | error | extract_failed | skipped (None when exc is set)
        self.http_status: int | None = None
        self.note: str | None = None
        self.docs: list[dict] = []
        self.descs: list[dict] = []
        self.form: str | None = None
        self.fallback_full = 0
        self.summary_only = 0
        self.skipped_unchanged = 0
        self.exc: BaseException | None = None
        self.duration_s = 0.0
        self.bytes = 0
        self.pdfs = 0
        self.pdf_bytes = 0


def needs_download(org: str, docs: Sequence[Mapping[str, Any]], stored: Mapping[tuple[str, str], str],
                   refresh: bool) -> bool:
    """False when fetch_reports would settle the company from `stored` alone (no PDF request): the first candidate
    already extracted ('ok') decides a skip, candidates that failed with the current extractor are passed over,
    and when all of them failed before there is nothing new to try."""
    if refresh:
        return bool(docs)
    for doc in docs:
        st = stored.get((org, doc["url"]))
        if st == "ok":
            return False
        if st != "failed":
            return True
    return False


def fetch_reports(cfg: Config, client: Any, org: str, code: str, group: Sequence[Mapping[str, Any]],
                  docs: Sequence[dict], kind: str, *, refresh: bool, stored: Mapping[tuple[str, str], str],
                  at: dt.datetime, rate_key: str = RATE_KEY) -> _Outcome:
    """Download + extract the selected documents of one company in order (summary, then the full report when the
    summary fails or only gave the management-discussion fallback), writing the section text files. Network +
    parsing only: the returned _Outcome is applied to the batch by the main thread."""
    out = _Outcome()
    try:
        return _fetch_reports(out, cfg, client, org, code, group, docs, kind, refresh, stored, at, rate_key)
    except (Blocked, Halted) as e:
        # as before: a blocked / halted company records nothing (it is redone by a later run)
        clean = _Outcome()
        clean.duration_s, clean.bytes, clean.pdfs, clean.pdf_bytes = out.duration_s, out.bytes, out.pdfs, out.pdf_bytes
        clean.exc = e
        return clean


def _fetch_reports(out: _Outcome, cfg: Config, client: Any, org: str, code: str, group: Sequence[Mapping[str, Any]],
                   docs: Sequence[dict], kind: str, refresh: bool, stored: Mapping[tuple[str, str], str],
                   at: dt.datetime, rate_key: str) -> _Outcome:
    notes: list[str] = []
    pending_rows: list[dict] = []
    primary = group[0]
    # a summary whose text came only from the management-discussion fallback is kept (text file + row) but the
    # full report is still tried for a real business section; soft = (row, section, doc, note, index)
    soft: tuple | None = None

    def get(url: str) -> Response:
        t = time.monotonic()
        try:
            r = client.get(url, rate_key=rate_key, max_bytes=MAX_PDF_BYTES, deadline_s=PDF_DEADLINE_S)
        finally:
            out.duration_s += time.monotonic() - t
        n = len(getattr(r, "body", b"") or b"")
        out.bytes += n
        if r.status == 200:
            out.pdfs += 1
            out.pdf_bytes += n
        return r

    def finish_ok(row: dict, sect: str, doc: dict, xnote: str, i: int) -> _Outcome:
        out.docs.extend(r for r in pending_rows if r is not row)
        out.docs.append(row)
        desc = short_description(sect)
        if desc:
            for ln in group:
                out.descs.append({"security_id": ln["security_id"], "source_id": SOURCE_ID,
                                  "company_key": ln["company_key"], "text": desc,
                                  "text_sha256": store.sha256(desc.encode("utf-8")), "lang": "zh",
                                  "source_url": doc["url"], "fetched_at": at, "match_method": ID_TYPE,
                                  "match_score": 1.0})
        if "summary_only" in (doc.get("flags") or ()):
            out.summary_only += 1                # kind='full' but the year has only a summary
        elif i > 0 or doc["kind"] != kind:
            out.fallback_full += 1
        out.status, out.http_status, out.note, out.form = "ok", 200, xnote, row["form"]
        return out

    def finish_soft(extra: str) -> _Outcome:
        row, sect, doc, xnote, i = soft
        return finish_ok(row, sect, doc, f"{xnote};{extra}", i)

    for i, doc in enumerate(docs):
        if not refresh and (org, doc["url"]) in stored:
            if stored[(org, doc["url"])] == "ok":
                out.docs.extend(pending_rows)   # a retried candidate that failed again is still recorded
                out.skipped_unchanged += 1
                out.status = "skipped"
                return out
            notes.append(f"{doc['kind']}_previously_failed")
            continue
        row = {"doc_id": doc_id_for(org, doc["announcement_id"]), "security_id": primary["security_id"],
               "company_key": primary["company_key"], "source_id": SOURCE_ID, "cik": org,
               "form": FORM_FOR_KIND[doc["kind"]], "section": SECTION, "accession": doc["announcement_id"],
               "filing_date": doc["filing_date"], "report_date": dt.date(doc["year"], 12, 31), "url": doc["url"],
               "raw_sha256": None, "raw_bytes": None, "text_path": None, "text_sha256": None,
               "text_chars": None, "extractor": f"{EXTRACTOR_VERSION}/none", "extract_note": None,
               "fetched_at": at}
        if doc["size_kb"] is not None and doc["size_kb"] * 1024 > MAX_PDF_BYTES:
            xnote, sect = f"too_large:{doc['size_kb']}KB", None
        else:
            try:
                dresp = get(doc["url"])
            except (OSError, http.client.HTTPException) as e:
                # RequestTimeout / network error on this PDF (Blocked / Halted propagate): a kept fallback
                # summary still stands, and rows of earlier candidates are recorded either way
                if soft is not None:
                    return finish_soft(f"{doc['kind']}_network:{type(e).__name__}")
                out.docs.extend(pending_rows)
                out.exc = e
                return out
            if dresp.status != 200:
                if soft is not None:
                    return finish_soft(f"{doc['kind']}_http_{dresp.status}")
                # keep what earlier candidates produced, but this company is an error for now
                out.docs.extend(pending_rows)
                out.status, out.http_status, out.note = "error", dresp.status, f"pdf_http_{dresp.status}"
                return out
            bad = non_pdf_body(dresp)
            if bad == "page":
                # a page instead of the PDF: a soft block (WAF / rate-limit page as HTTP 200). Stop the whole run
                # like a 403/429: halt at once, never record it as a failed extraction
                ev = getattr(client, "halt", None)
                if isinstance(ev, threading.Event):
                    ev.set()
                raise Blocked(getattr(dresp, "url", None) or doc["url"], dresp.status, "non_pdf_body")
            if bad is not None:
                if soft is not None:
                    return finish_soft(f"{doc['kind']}_not_pdf")
                # not a PDF and not a page (empty / unknown bytes): an error for now (retried by a later run,
                # counts toward the consecutive-error stop), never a failed extraction under this extractor
                out.docs.extend(pending_rows)
                out.status, out.http_status, out.note = "error", dresp.status, "pdf_not_pdf"
                return out
            raw = dresp.body or b""
            sect, xnote, backend = extract_from_pdf(raw, doc["kind"])
            if getattr(dresp, "truncated", False):
                xnote += ";truncated"
            row.update(raw_sha256=store.sha256(raw), raw_bytes=len(raw),
                       extractor=f"{EXTRACTOR_VERSION}/{backend}")
        if doc["revision"]:
            xnote += ";revision"
        for flag in doc.get("flags") or ():
            xnote += f";{flag}"
        if notes:
            xnote += ";" + ";".join(notes)
        row["extract_note"] = xnote
        if sect is None:
            pending_rows.append(row)
            notes.append(f"{doc['kind']}_failed:{xnote.split(';', 1)[0]}")
            continue
        tp = text_path_for(cfg, code, doc["announcement_id"])
        tp.parent.mkdir(parents=True, exist_ok=True)
        payload = sect.encode("utf-8")
        tp.write_bytes(payload)
        row.update(text_path=str(tp), text_sha256=store.sha256(payload), text_chars=len(sect))
        if "fallback:" in xnote and soft is None and i < len(docs) - 1:
            soft = (row, sect, doc, xnote, i)
            pending_rows.append(row)
            notes.append(f"{doc['kind']}_fallback")
            continue
        return finish_ok(row, sect, doc, xnote, i)
    if soft is not None:
        return finish_soft(notes[-1] if notes and notes[-1] != f"{soft[2]['kind']}_fallback" else "no_better")
    out.docs.extend(pending_rows)
    if not pending_rows:     # every candidate failed in an earlier run: nothing new to try
        out.skipped_unchanged += 1
        out.status, out.note = "skipped", "known_failed"
        return out
    out.status, out.http_status = "extract_failed", 200
    out.note = pending_rows[-1]["extract_note"] or "extract_failed"
    return out


class _DaemonPool:
    """Minimal executor on daemon threads (the tasks set their own Futures). Unlike ThreadPoolExecutor, whose
    threads are joined at interpreter exit, an in-flight PDF abandoned after DRAIN_MAX_S does not keep the process
    alive for up to PDF_DEADLINE_S after the run has reported; the OS closes its socket at exit."""

    def __init__(self, workers: int, name: str) -> None:
        self._q: queue_mod.SimpleQueue = queue_mod.SimpleQueue()
        self._threads = [threading.Thread(target=self._run, name=f"{name}-{i}", daemon=True)
                         for i in range(max(1, int(workers)))]
        for t in self._threads:
            t.start()

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            fn, args = item
            try:
                fn(*args)
            except BaseException:  # noqa: BLE001 - the task reports through its Future
                pass

    def submit(self, fn: Callable, *args: Any) -> None:
        self._q.put((fn, args))

    def shutdown(self, wait: bool = True) -> None:
        for _ in self._threads:
            self._q.put(None)
        if wait:
            for t in self._threads:
                t.join()


class _FirstBlock:
    """The run's first http.Blocked seen by a PDF worker: the cooldown marker is written by that worker at once
    (guard.mark_blocked never raises, atomic replace), before the main thread handles the result or waits for the
    database. Later blocks are ignored."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.lock = threading.Lock()
        self.error: Blocked | None = None
        self.url: str | None = None
        self.code: str | None = None
        self.marker: Any = None

    def record(self, e: Blocked, url: str | None, code: str) -> None:
        with self.lock:
            if self.error is not None:
                return
            self.error, self.url, self.code = e, (getattr(e, "url", None) or url), code
            self.marker = guard.mark_blocked(self.cfg, COMMAND, url=self.url, status=getattr(e, "status", None),
                                             reason=getattr(e, "reason", None))


def _halt_event(*clients: Any) -> threading.Event:
    """The clients' shared halt Event (http.Client.halt: no new request starts once set), else a private one."""
    for c in clients:
        ev = getattr(c, "halt", None)
        if isinstance(ev, threading.Event):
            return ev
    return threading.Event()


def _sync(cfg: Config, client: Any, *, limit, codes, kind, refresh, min_mcap_usd, batch_size, only_universe,
          progress_every, on_blocked, per_company, workers, since, max_fallback, pdf_client, list_min_queue,
          on_company=None) -> dict:
    run_id = f"sync-cninfo-{uuid.uuid4().hex[:12]}"
    req_clients = [client] if pdf_client is None or pdf_client is client else [client, pdf_client]
    requests_before = sum(int(getattr(c, "requests_made", 0) or 0) for c in req_clients)
    if not per_company and workers > 1 and getattr(pdf_client, "thread_safe", False) is not True:
        print(f"[cninfo] client {type(pdf_client).__name__} does not declare thread_safe; using 1 worker",
              file=sys.stderr, flush=True)
        workers = 1
    counts = {s: 0 for s in CRAWL_STATUSES}
    summary: dict[str, Any] = {
        "run_id": run_id, "kind": kind, "mode": "per_company" if per_company else "fast", "workers": workers,
        "stock_list_rows": 0, "cn_lines": 0, "securities_mapped": 0,
        "ambiguous": [], "unmatched": 0, "identifiers_removed": 0, "queued": 0, "attempted": 0,
        "skipped_unchanged": 0, "ok_by_form": {}, "fallback_full": 0, "summary_only": 0, "failures_by_note": {},
        "since": None, "list_windows": 0, "list_splits": 0, "list_pages": 0, "list_rows": 0,
        "list_announcements": 0, "list_complete": None, "list_usable": None, "list_stop": None,
        "list_total_reported": None, "list_shortfall": None, "days_over_cap": [], "list_gaps": [],
        "list_repairs": [], "list_split_mismatches": 0,
        "list_skipped": None, "list_s": None, "skipped_current": 0, "skipped_current_in_gap": 0,
        "gap_queries_planned": 0,
        "fallback_queries": 0, "no_annual_report_in_window": 0, "skipped_not_in_listing": 0,
        "skipped_older_year_only": 0,
        "requests": 0, "bytes_downloaded": 0, "pdfs": 0, "mean_pdf_kb": None, "companies_per_s": None,
        "snapshots": 0, "stopped_reason": None, "blocked_at": None, "blocked_url": None, "blocked_status": None,
        "blocked_reason": None, "abandoned_in_flight": 0, "status": None}

    def log(msg: str) -> None:
        print(f"[cninfo] {msg}", file=sys.stderr, flush=True)

    def blocked(url: str, e: Blocked) -> None:
        """Cooldown marker first (survives a failing DB write), then the caller's callback."""
        guard.mark_blocked(cfg, COMMAND, url=getattr(e, "url", None) or url, status=getattr(e, "status", None),
                           reason=getattr(e, "reason", None))
        if on_blocked is not None:
            try:
                on_blocked(e)
            except Exception:
                pass

    with store.session(cfg, wait_s=FINAL_WAIT_S) as con:
        con.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
                    [run_id, COMMAND, store.now_utc(), None, "running", 0, None])

    def finish(status: str, note: Any = None) -> None:
        summary["requests"] = sum(int(getattr(c, "requests_made", 0) or 0) for c in req_clients) - requests_before
        summary["status"] = status
        payload = json.dumps({"counts": counts, "stopped_reason": summary["stopped_reason"], "kind": kind,
                              "mode": summary["mode"], "workers": workers, "list_pages": summary["list_pages"],
                              "list_windows": summary["list_windows"], "list_rows": summary["list_rows"],
                              "days_over_cap": summary["days_over_cap"],
                              "skipped_current": summary["skipped_current"],
                              "gap_queries_planned": summary["gap_queries_planned"],
                              "list_repairs": summary["list_repairs"],
                              "fallback_queries": summary["fallback_queries"],
                              "no_annual_report_in_window": summary["no_annual_report_in_window"],
                              **({"blocked_url": summary["blocked_url"]} if summary["blocked_url"] else {}),
                              **({"note": str(note)} if note else {})}, default=str, ensure_ascii=False)
        with store.session(cfg, wait_s=FINAL_WAIT_S) as con:
            con.execute("UPDATE runs SET finished_at = ?, status = ?, requests = ?, note = ? WHERE run_id = ?",
                        [store.now_utc(), status, summary["requests"], payload, run_id])

    # ---- (1) stock list ---------------------------------------------------------------------------------
    t0 = time.monotonic()
    fetched_at = store.now_utc()
    try:
        resp = client.get(STOCK_LIST_URL, rate_key=RATE_KEY)
        summary["bytes_downloaded"] += len(resp.body or b"")
    except Blocked as e:
        blocked(STOCK_LIST_URL, e)
        summary.update(stopped_reason="blocked", blocked_at=STOCK_LIST_URL)
        finish("blocked", f"stock_list: {e.reason}")
        return summary
    except KeyboardInterrupt:
        summary["stopped_reason"] = "interrupted"
        finish("interrupted")
        return summary
    except (OSError, http.client.HTTPException, Halted) as e:
        summary["stopped_reason"] = "stock_list_error"
        finish("error", _err_note("stock_list: ", e))
        return summary
    if resp.status != 200:
        summary["stopped_reason"] = "stock_list_error"
        finish("error", f"stock_list: http_{resp.status}")
        return summary
    try:
        stock_rows = parse_stock_list(resp.body)
    except (ValueError, AttributeError, TypeError) as e:
        summary["stopped_reason"] = "stock_list_error"
        finish("error", _err_note("stock_list: bad_json: ", e))
        return summary
    summary["stock_list_rows"] = len(stock_rows)
    report: dict = {}
    try:
        raw_path, digest = store.save_raw(cfg, SOURCE_ID, "stock_list", resp.body)
        with store.session(cfg, wait_s=FINAL_WAIT_S) as con:
            list_snap = store.record_snapshot(
                con, source_id=SOURCE_ID, kind="stock_list",
                request={"method": "GET", "url": STOCK_LIST_URL, "rate_key": RATE_KEY}, raw_path=raw_path,
                raw_sha256=digest, raw_bytes=len(resp.body), rows=len(stock_rows),
                duration_s=round(time.monotonic() - t0, 3), fetched_at=fetched_at)
            summary["snapshots"] += 1
            lines = _load_lines(con, only_universe)
            mapping = map_securities_to_orgid(lines, stock_rows, report=report)
            store.upsert_many(con, "identifiers", ["security_id", "id_type", "id_value", "method", "snapshot_id"],
                              [[sid, ID_TYPE, org, method, list_snap] for sid, org, _, method in mapping])
            mapped = {sid for sid, _, _, _ in mapping}
            unmapped = sorted({ln["security_id"] for ln in lines} - mapped)
            if unmapped:
                summary["identifiers_removed"] = con.execute(
                    "SELECT count(*) FROM identifiers WHERE id_type = ? AND list_contains(?::VARCHAR[], security_id)",
                    [ID_TYPE, unmapped]).fetchone()[0]
                con.execute("DELETE FROM identifiers WHERE id_type = ? AND list_contains(?::VARCHAR[], security_id)",
                            [ID_TYPE, unmapped])
            # (org_id, url) -> 'ok' (text extracted: never fetched again) | 'failed' (failed with the current
            # EXTRACTOR_VERSION: not retried). Rows that failed under an older extractor are left out, so an improved
            # extractor retries them without --refresh.
            stored: dict[tuple[str, str], str] = {}
            # org -> newest fiscal year with a section extracted by the current EXTRACTOR_VERSION (the fast-mode
            # pre-query skip: a company already holding the newest year that can exist is not queried). For
            # --kind full only full reports count (a stored summary does not make the full report current).
            current_year: dict[str, int] = {}
            for org, url, tp, ext, rdate, form in con.execute(
                    "SELECT cik, url, text_path, extractor, report_date, form FROM documents WHERE source_id = ?",
                    [SOURCE_ID]).fetchall():
                key = (str(org), str(url))
                if tp is not None:
                    stored[key] = "ok"
                    if str(ext or "").startswith(EXTRACTOR_VERSION + "/") and isinstance(rdate, dt.date) and \
                            (kind == "summary" or form == FORM_FOR_KIND["full"]):
                        current_year[str(org)] = max(current_year.get(str(org), 0), rdate.year)
                elif str(ext or "").startswith(EXTRACTOR_VERSION + "/"):
                    stored.setdefault(key, "failed")
            prior = con.execute("SELECT security_id, attempts, status FROM crawl_state WHERE source_id = ?",
                                [SOURCE_ID]).fetchall()
            attempts = {sid: n for sid, n, _ in prior}
            prior_status = {sid: st for sid, _, st in prior}
    except KeyboardInterrupt:
        summary["stopped_reason"] = "interrupted"
        finish("interrupted")
        return summary
    summary["cn_lines"] = len(lines)
    summary["securities_mapped"] = len(mapping)
    summary["ambiguous"] = report.get("ambiguous", [])
    summary["unmatched"] = report.get("unmatched", 0)

    # ---- (2) queue: distinct orgIds, largest market cap first ----------------------------------------------
    by_sid = {ln["security_id"]: ln for ln in lines}
    groups: dict[str, list[dict]] = {}
    code_of: dict[str, str] = {}
    for sid, org, code, _ in mapping:
        groups.setdefault(org, []).append(dict(by_sid[sid], code=code))
    for org, g in groups.items():
        g.sort(key=lambda ln: (is_b_share(ln["code"]), -(ln["market_cap_usd"] or float("-inf")), ln["security_id"]))
        code_of[org] = g[0]["code"]     # the A-share code when the company has one
    cap = {org: max((ln["market_cap_usd"] for ln in g if ln["market_cap_usd"] is not None), default=None)
           for org, g in groups.items()}
    queue = sorted(groups, key=lambda o: (-(cap[o] if cap[o] is not None else float("-inf")), o))
    if codes is not None:
        queue = [o for o in queue if any(ln["code"] in codes or ln["security_id"].upper().rsplit(":", 1)[-1] in codes
                                         for ln in groups[o])]
    if min_mcap_usd is not None:
        queue = [o for o in queue if cap[o] is not None and cap[o] >= float(min_mcap_usd)]
    if limit is not None:
        queue = queue[:max(0, int(limit))]
    summary["queued"] = len(queue)

    batch = _Batch()
    consecutive_errors = 0
    locked_flushes = 0
    next_flush_at = batch_size
    meter = {"pdfs": 0, "pdf_bytes": 0, "done": 0, "fetched": 0}
    t_phase = t_start = time.monotonic()

    def event(org: str, status: str, note: str | None) -> None:
        if on_company is not None:
            try:
                on_company([ln["security_id"] for ln in groups[org]], status, note)
            except Exception:  # noqa: BLE001 - a callback never stops the sync
                pass

    def state(org: str, status: str, http_status: int | None, note: str | None, at: dt.datetime) -> None:
        for ln in groups[org]:
            sid = ln["security_id"]
            batch.states.append([SOURCE_ID, sid, status, http_status, (attempts.get(sid) or 0) + 1, at, note])
        counts[status] += 1
        if status != "ok":
            key = (note or status).split(":", 1)[0].split(";", 1)[0].strip() or status
            summary["failures_by_note"][key] = summary["failures_by_note"].get(key, 0) + 1
        event(org, status, note)

    def new_batch() -> _Batch:
        b = _Batch()
        b.pdf_rate_key = RATE_KEY if per_company else STATIC_RATE_KEY
        return b

    def flush(note: str | None = None, *, wait_s: float = FINAL_WAIT_S) -> None:
        nonlocal batch
        summary["snapshots"] += _flush(cfg, batch, attempts, note, wait_s=wait_s)
        batch = new_batch()

    def flush_mid_run() -> None:
        nonlocal locked_flushes, next_flush_at
        try:
            flush(wait_s=FLUSH_WAIT_S)
        except store.StoreLocked:
            locked_flushes += 1
            next_flush_at = batch.groups + batch_size
            log(f"store locked by another process; keeping {batch.groups} pending companies "
                f"(locked flush {locked_flushes}/{MAX_LOCKED_FLUSHES})")
            if locked_flushes >= MAX_LOCKED_FLUSHES or batch.groups >= MAX_PENDING:
                summary["stopped_reason"] = "store_locked"
            return
        locked_flushes = 0
        next_flush_at = batch_size

    batch = new_batch()

    def timed(fn, *a, **kw):
        t = time.monotonic()
        try:
            r = fn(*a, **kw)
        finally:
            batch.duration_s += time.monotonic() - t
        summary["bytes_downloaded"] += len(getattr(r, "body", b"") or b"")
        return r

    def rate() -> float:
        el = time.monotonic() - t_phase
        return meter["done"] / el if el > 0 else 0.0

    def progress() -> None:
        if progress_every and meter["done"] % progress_every == 0:
            log(f"{meter['done']}/{len(queue)} {counts} companies/s={rate():.2f}")

    def query_company(org: str, at: dt.datetime) -> Mapping | None:
        """The per-company announcement query (rate_key 'cninfo'); None when it failed (state recorded)."""
        code = code_of[org]
        form = query_form(code, org)
        resp = timed(post_form, client, QUERY_URL, form, headers=QUERY_HEADERS, rate_key=RATE_KEY)
        if resp.status != 200:
            state(org, "error", resp.status, f"query_http_{resp.status}", at)
            return None
        try:
            data = json.loads(resp.body)
            if not isinstance(data, Mapping):
                raise ValueError("not an object")
        except ValueError:
            state(org, "error", resp.status, "query_bad_json", at)
            return None
        path, dig = store.save_raw(cfg, SOURCE_ID, f"query-{code}", resp.body)
        batch.queries.append({"org_id": org, "code": code, "form": form, "raw_path": path, "raw_sha256": dig,
                              "raw_bytes": len(resp.body), "fetched_at": at.isoformat()})
        return data

    def select_for(org: str, announcements: Any, at: dt.datetime) -> list[dict] | None:
        sel: dict = {}
        docs = select_annual_reports(announcements, kind, report=sel)
        if not docs:
            state(org, "no_annual_report", 200, sel.get("note") or "no_annual_report", at)
            return None
        return docs

    def apply(org: str, out: _Outcome, at: dt.datetime) -> str | None:
        """Record one company's report outcome (main thread only). Returns its crawl status, 'skipped', or None
        when out.exc is Blocked / Halted (the caller handles those)."""
        batch.docs.extend(out.docs)
        batch.descs.extend(out.descs)
        batch.duration_s += out.duration_s
        summary["bytes_downloaded"] += out.bytes
        meter["fetched"] += bool(out.pdfs)
        meter["pdfs"] += out.pdfs
        meter["pdf_bytes"] += out.pdf_bytes
        summary["fallback_full"] += out.fallback_full
        summary["summary_only"] += out.summary_only
        summary["skipped_unchanged"] += out.skipped_unchanged
        if out.form:
            summary["ok_by_form"][out.form] = summary["ok_by_form"].get(out.form, 0) + 1
        if isinstance(out.exc, (OSError, http.client.HTTPException)):
            state(org, "error", None, _err_note("network: ", out.exc), at)
            return "error"
        if out.exc is not None:
            return None
        if out.status in ("ok", "error", "extract_failed"):
            state(org, out.status, out.http_status, out.note, at)
        elif out.status == "skipped":
            event(org, "skipped_known_failed" if out.note == "known_failed" else "skipped_unchanged", None)
        return out.status

    def report_block(e: Blocked, url: str | None, code: str, *, marker_written: bool = False) -> None:
        """First block of the run: summary fields; marker (unless the worker wrote it) and on_blocked."""
        if summary["blocked_url"] is not None:
            return
        first_url = getattr(e, "url", None) or url
        summary.update(stopped_reason="blocked", blocked_at=code, blocked_url=first_url,
                       blocked_status=getattr(e, "status", None), blocked_reason=getattr(e, "reason", None))
        if marker_written:
            if on_blocked is not None:
                try:
                    on_blocked(e)
                except Exception:
                    pass
        else:
            blocked(first_url, e)

    # ---- (3a) per-company path (the old sequential loop) --------------------------------------------------
    def process(org: str) -> str:
        """Network + parsing for one company. Returns the crawl status recorded (or 'skipped')."""
        at = store.now_utc()
        batch.started_at = batch.started_at or at
        data = query_company(org, at)
        if data is None:
            return "error"
        docs = select_for(org, data.get("announcements"), at)
        if docs is None:
            return "no_annual_report"
        out = fetch_reports(cfg, client, org, code_of[org], groups[org], docs, kind, refresh=refresh,
                            stored=stored, at=at, rate_key=RATE_KEY)
        status = apply(org, out, at)
        if isinstance(out.exc, (Blocked, Halted)):
            raise out.exc
        return status

    def run_per_company() -> None:
        nonlocal consecutive_errors
        for org in queue:
            summary["attempted"] += 1
            try:
                status = process(org)
            except Blocked as e:
                report_block(e, QUERY_URL, code_of[org])       # cooldown marker first
                state(org, "blocked", e.status, f"blocked: {e.reason}", store.now_utc())
                break
            except Halted:
                summary["stopped_reason"] = "interrupted"
                break
            except (OSError, http.client.HTTPException) as e:
                state(org, "error", None, _err_note("network: ", e), store.now_utc())
                status = "error"
            consecutive_errors = consecutive_errors + 1 if status == "error" else 0
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                summary["stopped_reason"] = "consecutive_errors"
                break
            batch.groups += 1
            meter["done"] = summary["attempted"]
            if batch.groups >= next_flush_at:
                flush_mid_run()
                if summary["stopped_reason"]:
                    break
            progress()

    # ---- (3b) fast path: bulk listing + PDF worker pool ----------------------------------------------------
    def run_fast() -> None:
        nonlocal consecutive_errors, t_phase
        halt = _halt_event(pdf_client, client)
        until = _today()
        start = since or default_since(until)
        floor = settle_floor(until)
        summary["since"] = start.isoformat()
        by_org: dict[str, list[dict]] = {}
        by_code: dict[str, list[dict]] = {}
        listing: RangeListing | None = None
        min_queue = LIST_MIN_QUEUE if list_min_queue is None else max(0, int(list_min_queue))
        if codes is not None or len(queue) <= min_queue:
            # the listing costs ~ its page count in requests (~420 at 1 req/s); one query per company is cheaper
            why = "--codes given" if codes is not None else f"queue {len(queue)} <= {min_queue}"
            summary["list_skipped"] = why
            log(f"listing skipped ({why}): one announcement query per company on {RATE_KEY}, PDFs on "
                f"{STATIC_RATE_KEY} with {workers} worker(s)")
        else:
            if start > floor:
                log(f"WARNING: --since {start} is after {floor}: the listing cannot settle companies whose newest "
                    f"report is FY{floor.year - 1}; they need per-company queries (capped at {max_fallback})")
            listing = RangeListing(start, until)
            batch.listing = listing          # snapshotted at the next flush, also when the pass is cut short
            log(f"listing annual reports {listing.se_date} in date windows of <= {WINDOW_MAX_ROWS} rows (CNINFO "
                f"cap {PAGE_CAP_PAGES} pages per query; rate_key {RATE_KEY}, >= {DEFAULT_MIN_INTERVAL_S:g} s apart)")
            try:
                list_range(client, cfg, listing, log=log)
            except Blocked as e:
                report_block(e, QUERY_URL, "listing")
                return
            except Halted:
                summary["stopped_reason"] = "interrupted"
                return
            except (OSError, http.client.HTTPException) as e:
                listing.stop, listing.error, listing.complete = "error", _err_note("list_network: ", e), False
                listing.usable = False
            finally:
                summary.update(list_pages=listing.pages, list_windows=len(listing.leaves),
                               list_splits=listing.splits, list_rows=len(listing.announcements),
                               list_announcements=len(listing.announcements), list_complete=listing.complete,
                               list_usable=listing.usable, list_stop=listing.stop,
                               list_total_reported=listing.total_reported, list_shortfall=listing.shortfall,
                               days_over_cap=list(listing.days_over_cap),
                               list_gaps=[f"{a}~{b}:{r}" for a, b, r in listing.gaps],
                               list_repairs=list(listing.repairs),
                               list_split_mismatches=len(listing.split_mismatches))
                summary["bytes_downloaded"] += listing.bytes
                summary["list_s"] = round(time.monotonic() - t_start, 1)
            log(f"listing done: {len(listing.leaves)} window(s) ({listing.splits} split probe(s)), "
                f"{listing.pages} pages, {len(listing.announcements)} announcements (windows reported "
                f"{listing.total_reported}, whole range {listing.total_first}), stop={listing.stop}"
                + (f", days over the cap: {', '.join(listing.days_over_cap)}" if listing.days_over_cap else "")
                + (f", repaired: {', '.join(listing.repairs)}" if listing.repairs else "")
                + (f", gaps: {', '.join(f'{a}~{b}:{r}' for a, b, r in listing.gaps)}" if listing.gaps else ""))
            if listing.usable:
                by_org, by_code = listing.groups()
            else:
                # a cut-short pass proves nothing, not even for companies it did see (a revision or the full
                # report may sit on a missed page): every company that is not current gets its query, no cap
                log(f"listing unusable (stop={listing.stop}, error={listing.error}): not used; every company not "
                    f"already current gets its per-company query (no cap)")
        t_phase = time.monotonic()
        usable = listing is not None and listing.usable
        newest_possible = newest_possible_year(until)

        # ---- plan: listing rows per company, and which unsettled companies get the capped per-company query
        # org -> listing rows when they settle it, SKIP_CURRENT (stored report is the newest year that can exist:
        # no request; not with --codes, an explicit recheck), else None (per-company query)
        plan: dict[str, Any] = {}
        unsettled: list[str] = []
        gap_orgs: set[str] = set()            # could have a row in an incomplete window: query outside the cap
        has_rows: set[str] = set()
        for org in queue:
            anns = company_announcements(by_org, by_code, org, [ln["code"] for ln in groups[org]]) if usable else []
            if anns:
                has_rows.add(org)
            in_gap = usable and listing.gap_affects(anns)
            if usable and not in_gap and window_settles(anns, start):
                plan[org] = anns              # settled by the listing (also when current: revisions are seen)
            elif not refresh and codes is None and (current_year.get(org) or 0) >= newest_possible:
                plan[org] = SKIP_CURRENT
                if in_gap:                    # a revision inside the gap is not seen (the pre-query trade-off)
                    summary["skipped_current_in_gap"] += 1
            else:
                plan[org] = None
                unsettled.append(org)
                if in_gap:
                    gap_orgs.add(org)
        summary["gap_queries_planned"] = len(gap_orgs)
        cap = max_fallback if usable else None
        allowed: set[str] = set(unsettled)
        capped = [o for o in unsettled if o not in gap_orgs]
        if cap is not None and len(capped) > cap:
            exempt: set[str] = set(gap_orgs)
            bj_queued = [o for o in capped if market_of_code(code_of[o]) == "BJ"]
            if bj_queued and not any(market_of_code(str(a.get("secCode") or "")) == "BJ"
                                     for a in listing.announcements):
                exempt |= set(bj_queued)
                log(f"WARNING: the listing has no Beijing (4/8/92xxxx) rows; {len(bj_queued)} queued Beijing "
                    f"companies get their per-company query outside the {cap} cap")

            def known_empty(org: str) -> bool:     # an earlier run found no annual report: after the others
                return all(prior_status.get(ln["security_id"]) == "no_annual_report" for ln in groups[org])
            rest = sorted((o for o in capped if o not in exempt),
                          key=lambda o: known_empty(o))            # stable: market-cap order within each group
            allowed = exempt | set(rest[:cap])
        n_current = sum(1 for o in queue if plan[o] is SKIP_CURRENT)
        if n_current:
            log(f"{n_current} companies already hold a FY{newest_possible}+ report (extractor {EXTRACTOR_VERSION}) "
                f"and are not queried (skipped_current"
                + (f"; {summary['skipped_current_in_gap']} of them could have a revision in a listing gap"
                   if summary["skipped_current_in_gap"] else "") + ")")
        if gap_orgs:
            log(f"{len(gap_orgs)} companies could have a report in an incomplete listing window: per-company "
                f"query outside the {max_fallback} cap")
        skipped_codes: list[str] = []

        block = _FirstBlock(cfg)
        pool = _DaemonPool(workers, "cninfo-pdf")
        inflight: dict[int, tuple[str, dt.datetime, Future]] = {}
        handled: set[int] = set()
        done_seq = itertools.count()
        tokens = itertools.count()
        pending = iter(queue)
        raised: list[BaseException] = []

        def stopping() -> bool:
            return bool(summary["stopped_reason"] or raised or halt.is_set())

        def complete(status: str | None) -> None:
            """Completion-order bookkeeping of one company (main thread)."""
            nonlocal consecutive_errors
            if status in ("error",):
                consecutive_errors += 1
            elif status is not None:
                consecutive_errors = 0
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS and not summary["stopped_reason"]:
                summary["stopped_reason"] = "consecutive_errors"
                halt.set()
            batch.groups += 1
            meter["done"] += 1
            progress()

        def prepare(org: str) -> tuple[list[dict], dt.datetime] | None:
            """Main thread: the company's announcements (listing rows that settle it, else the per-company query
            when the plan allows it), the selection, and every outcome that needs no PDF. Returns (docs, at) for a
            PDF task, else None."""
            anns: Any = plan[org]
            if anns is SKIP_CURRENT:
                summary["skipped_current"] += 1
                event(org, "skipped_current", None)
                meter["done"] += 1
                progress()
                return None
            if anns is None and org not in allowed:
                summary["no_annual_report_in_window"] += 1
                key = "older_year_only" if org in has_rows else "not_in_listing"
                summary[f"skipped_{key}"] += 1
                if len(skipped_codes) < 50:
                    skipped_codes.append(code_of[org])
                meter["done"] += 1
                progress()
                return None
            summary["attempted"] += 1
            at = store.now_utc()
            batch.started_at = batch.started_at or at
            if anns is None:
                summary["fallback_queries"] += 1
                data = query_company(org, at)       # Blocked / Halted / network errors: handled by fill()
                if data is None:
                    complete("error")
                    return None
                anns = data.get("announcements")
            docs = select_for(org, anns, at)
            if docs is None:
                complete("no_annual_report")
                return None
            if not needs_download(org, docs, stored, refresh):
                out = fetch_reports(cfg, pdf_client, org, code_of[org], groups[org], docs, kind, refresh=refresh,
                                    stored=stored, at=at, rate_key=STATIC_RATE_KEY)   # settled locally: no request
                complete(apply(org, out, at))
                return None
            return docs, at

        def task(fut: Future, org: str, docs: list[dict], at: dt.datetime) -> None:
            """Worker: fetch + extract one company's PDFs (no database, no shared counters)."""
            if not fut.set_running_or_notify_cancel():   # cancelled before it started: nothing is sent
                return
            try:
                if halt.is_set():
                    out = _Outcome()
                    out.exc = Halted(docs[0]["url"] if docs else None)
                else:
                    out = fetch_reports(cfg, pdf_client, org, code_of[org], groups[org], docs, kind,
                                        refresh=refresh, stored=stored, at=at, rate_key=STATIC_RATE_KEY)
                if isinstance(out.exc, Blocked):
                    halt.set()                           # http.Client already set it; fakes rely on this
                    block.record(out.exc, getattr(out.exc, "url", None), code_of[org])
                fut.set_result((next(done_seq), "out", out))
            except BaseException as e:   # noqa: BLE001 - re-raised by the main thread after draining
                fut.set_result((next(done_seq), "raised", e))

        def fill() -> None:
            while len(inflight) < workers and not stopping():
                org = next(pending, None)
                if org is None:
                    return
                try:
                    prep = prepare(org)
                except Blocked as e:
                    halt.set()
                    report_block(e, QUERY_URL, code_of[org])   # cooldown marker first
                    state(org, "blocked", e.status, f"blocked: {e.reason}", store.now_utc())
                    return
                except Halted:
                    summary["attempted"] -= 1
                    return
                except (OSError, http.client.HTTPException) as e:
                    state(org, "error", None, _err_note("network: ", e), store.now_utc())
                    complete("error")
                    prep = None
                if prep is None:
                    for token, kind_, value in wait_results(0):    # results that finished meanwhile
                        handle(token, kind_, value)
                    if batch.groups >= next_flush_at:
                        flush_mid_run()
                        if summary["stopped_reason"]:
                            halt.set()
                    continue
                docs, at = prep
                fut: Future = Future()
                inflight[next(tokens)] = (org, at, fut)    # registered before submit: a result is never lost
                pool.submit(task, fut, org, docs, at)

        def drop_unstarted() -> None:
            for token, entry in list(inflight.items()):
                if entry[2].cancel():
                    inflight.pop(token, None)
                    summary["attempted"] -= 1

        def wait_results(timeout: float) -> list[tuple[int, str, Any]]:
            by_future = {entry[2]: token for token, entry in inflight.items()}
            if not by_future:
                return []
            done, _ = futures_wait(list(by_future), timeout=timeout, return_when=FIRST_COMPLETED)
            finished = []
            for fut in done:
                token = by_future[fut]
                if fut.cancelled():
                    if inflight.pop(token, None) is not None:
                        summary["attempted"] -= 1
                    continue
                seq, kind_, value = fut.result()
                finished.append((seq, token, kind_, value))
            finished.sort(key=lambda x: x[0])
            return [(token, kind_, value) for _, token, kind_, value in finished]

        def handle(token: int, kind_: str, value: Any) -> None:
            entry = inflight.get(token)
            if entry is None:
                return
            if token in handled:
                inflight.pop(token, None)
                return
            handled.add(token)              # before record(): an interrupt inside it never records it twice
            inflight.pop(token, None)
            record(entry, kind_, value)

        def record(entry: tuple, kind_: str, value: Any) -> None:
            org, at, _ = entry
            if kind_ == "raised":
                halt.set()
                raised.append(value)
                return
            out: _Outcome = value
            if isinstance(out.exc, Halted):          # never started: left for a later run
                summary["attempted"] -= 1
                return
            if isinstance(out.exc, Blocked):
                e = out.exc
                apply(org, out, at)
                state(org, "blocked", e.status, f"blocked: {e.reason}", at)
                first = block.error or e
                if summary["blocked_url"] is None:
                    report_block(first, block.url or e.url, block.code or code_of[org],
                                 marker_written=block.marker is not None)
                    log(f"blocked at {summary['blocked_url']} (status={summary['blocked_status']}, "
                        f"reason={summary['blocked_reason']}); stopping the run, draining {len(inflight)} "
                        f"in-flight PDF task(s), flushing progress")
                return
            complete(apply(org, out, at))

        try:
            fill()
            while inflight:
                for token, kind_, value in wait_results(RESULT_POLL_S):
                    handle(token, kind_, value)
                if stopping():
                    halt.set()           # no new start on either key; the loop only drains what is in flight
                    continue
                if batch.groups >= next_flush_at:
                    flush_mid_run()
                    if summary["stopped_reason"]:
                        halt.set()
                        continue
                fill()
            if raised:
                raise raised[0]
        except BaseException:
            halt.set()
            raise
        finally:
            try:
                if inflight:
                    halt.set()
                    drop_unstarted()
                deadline = time.monotonic() + DRAIN_MAX_S
                while inflight:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        log(f"drain limit {DRAIN_MAX_S:g} s reached; {len(inflight)} in-flight PDF task(s) left "
                            f"unrecorded (redone next run)")
                        break
                    for token, kind_, value in wait_results(min(RESULT_POLL_S, left)):
                        handle(token, kind_, value)
                    drop_unstarted()
            except KeyboardInterrupt:     # a second Ctrl-C while draining: stop waiting
                halt.set()
            summary["abandoned_in_flight"] = len(inflight)
            if block.error is not None and summary["blocked_url"] is None:
                report_block(block.error, block.url, block.code or "?", marker_written=block.marker is not None)
            if summary["stopped_reason"] is None and halt.is_set() and not raised:
                summary["stopped_reason"] = "interrupted"
            summary["no_annual_report_in_window_codes"] = skipped_codes
            pool.shutdown(wait=not inflight)       # daemon threads: exit never waits for an abandoned PDF

    try:
        if per_company:
            run_per_company()
        else:
            run_fast()
    except KeyboardInterrupt:
        if summary["stopped_reason"] != "blocked":
            summary["stopped_reason"] = "interrupted"
    except BaseException:
        was_blocked = summary["stopped_reason"] == "blocked"
        if not was_blocked:
            summary["stopped_reason"] = "crashed"
        with sigterm_ignored():
            try:
                flush(note="run stopped: blocked (crashed while stopping)" if was_blocked else "run stopped: crashed")
            finally:
                try:
                    finish("blocked" if was_blocked else "error", "crashed")
                finally:
                    if was_blocked:
                        log(f"BLOCKED url={summary['blocked_url']} status={summary['blocked_status']} "
                            f"reason={summary['blocked_reason']}")
        raise
    run_status = {"blocked": "blocked", "consecutive_errors": "stopped_errors", "interrupted": "interrupted",
                  "store_locked": "store_locked"}.get(summary["stopped_reason"], "ok")
    skipped = summary["no_annual_report_in_window"]
    if skipped and skipped > SKIPPED_WARN_SHARE * max(1, len(queue)):
        log(f"WARNING: {skipped} of {len(queue)} companies were not checked (fallback cap {max_fallback}: "
            f"{summary['skipped_not_in_listing']} not in the listing, {summary['skipped_older_year_only']} with only "
            f"an older year in it); run status 'partial'. Rerun with an earlier --since, --per-company, or a larger "
            f"cap")
        if run_status == "ok":
            run_status = "partial"
    summary.update({f"status_{k}": v for k, v in counts.items()})
    now = time.monotonic()
    elapsed = now - t_phase
    total = now - t_start
    # companies/s counts every company handled, also skipped / already-done ones (it overstates the download rate
    # on a resume); fetched_companies_per_s counts only companies with a PDF downloaded; total_s includes the
    # stock list and the listing
    summary.update(pdfs=meter["pdfs"], elapsed_s=round(elapsed, 1), total_s=round(total, 1),
                   companies_per_s=round(meter["done"] / elapsed, 3) if elapsed > 0 else None,
                   fetched_companies=meter["fetched"],
                   fetched_companies_per_s=round(meter["fetched"] / total, 3) if total > 0 else None,
                   mean_pdf_kb=round(meter["pdf_bytes"] / meter["pdfs"] / 1024, 1) if meter["pdfs"] else None)
    with sigterm_ignored():   # a SIGTERM here would roll back the last batch while 'ok' is recorded
        try:
            flush(note=f"run stopped: {summary['stopped_reason']}" if summary["stopped_reason"] else None)
        finally:
            try:
                finish(run_status)
            finally:
                if summary["stopped_reason"] == "blocked":
                    log(f"BLOCKED url={summary['blocked_url']} status={summary['blocked_status']} "
                        f"reason={summary['blocked_reason']}")
    return summary
