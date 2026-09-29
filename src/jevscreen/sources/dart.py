"""OpenDART adapter: TradingView KRX lines -> DART corp_code -> latest 사업보고서 -> '1. 사업의 개요' (L2 evidence).

Source (provenance.py): 'dart_business_report' (official-private). The section text is kept in local files under
<home>/docs/dart/<corp_code>/ and never shipped; only metadata goes into `documents`.

Flow (mirrors sources/sec_edgar.py; the run engine is shared with sources/edinet.py):
1. cfg.opendart_api_key() is required (DartApiKeyMissing, raised before any request). The key travels as the
   `crtfc_key` query parameter and is NEVER printed, logged or stored: recorded URLs are key-free and every error
   text / note / marker is redacted to '<opendart-api-key>' before it is truncated or written.
2. corpCode.xml (ZIP with CORPCODE.xml: corp_code, corp_name, corp_eng_name, stock_code, modify_date) maps KRX lines
   (6-character codes) to corp_code by stock_code. Several corp codes on one stock code -> ambiguous, unmapped.
   identifiers rows: id_type 'dart_corp_code', method 'stock_code'. Before downloading, the newest raw copy saved
   by an earlier run (<raw>/dart_business_report/<YYYY-MM-DD>/dart_corpcode-*.zip) that is <= CORPCODE_REUSE_DAYS
   (7) days old and parses is reused (snapshot note 'reused_raw:<path>', summary corpcode_reused = 1).
   Reused or downloaded, a list with < CORPCODE_MIN_ROWS (50,000) corp codes or < CORPCODE_MIN_STOCK_CODES (2,000)
   stock codes is refused (a partial / wrong file that still parses would drop the identifiers of every KRX line it
   misses): a suspect cache copy is skipped; a suspect download stops the run ('corpcode_error', summary
   corpcode_problem 'corpcode_suspect:rows=..;stock_codes=..'), nothing saved, no identifier touched.
3. Per company: list.json?corp_code=..&bgn_de=..&pblntf_ty=A&pblntf_detail_ty=A001 (사업보고서) over the last
   LOOKBACK_DAYS days; the report for the newest period ('사업보고서 (2024.12)') wins, the original preferred over
   a correction ('[기재정정]사업보고서 ...') of the same period (the correction is used only without an original).
4. Section text, mode 'web' (default; measured 2026-09-26: OpenDART throttles large downloads to ~9.5 KB/s per
   connection, a document.xml got 573 KB in 60 s, while the DART website answers a 112 KB main.do in 2.0 s and a
   4.4 KB section in 0.9 s):
   a. GET https://dart.fss.or.kr/dsaf001/main.do?rcpNo=<rcept_no> (rate_key 'dart-web', >= WEB_MIN_INTERVAL_S = 1 s
      between web request starts, deadline WEB_DEADLINE_S = 60 s). Its makeToc() script holds the table of contents
      as JS objects: `var node2 = {}; node2['text'] = "1. 사업의 개요"; node2['dcmNo'] = ..; node2['eleId'] = ..;
      node2['offset'] = ..; node2['length'] = ..; node2['dtd'] = ..` (node1 = chapter, node2/node3 nested items;
      parse_toc() reads them in any field order). No TOC node -> crawl_state 'error', note 'no_toc'.
   b. find_business_sections(): under the chapter 'II. 사업의 내용' the items '사업의 개요' and '주요 제품 및 서비스'
      ('주요 제품, 서비스 등', ...), titles compared without numbering ('1.', '가.', 'II.'), spaces or a parenthesised
      prefix ('(금융업)'). Each is fetched from https://dart.fss.or.kr/report/viewer.do?rcpNo=..&dcmNo=..&eleId=..
      &offset=..&length=..&dtd=.. (rate_key 'dart-web') and turned into text (dart_html_to_text: tables -> one line
      per row, cells joined by ' | ', block tags inside a cell a space, nested tables inside-out, paragraph breaks
      kept; the leading <P class='section-N'> title, also one broken by <BR>, dropped). A 200 answer must look like
      a section (section_verified: a section-N element or the node title in its first lines), else it is an HTTP
      200 error / throttling page: crawl_state 'error' note 'section_unverified;bytes:..;sha:..;title:..', no
      documents row (retried next run). Numbering without a dot ('II 사업의 내용', '1 사업의 개요') is accepted;
      several overview / products candidates keep the first, noted 'multiple_overview:<n>' / 'multiple_products:<n>'.
   c. text = 사업의 개요 + '\n\n【주요 제품 및 서비스】\n' + products (products capped at PRODUCTS_MAX_CHARS = 8,000);
      the minimum length and the short description use 사업의 개요 alone. Without a '사업의 개요' item, the whole
      chapter is fetched when it has a range (capped at CHAPTER_MAX_CHARS, note 'fallback:chapter'). A failed
      products fetch (HTTP error / deadline / stall / network / unverified page) keeps the overview (note
      'products_http_<n>', 'products_deadline', 'products_stall', 'products_network:..', 'products_unverified').
   d. extractor 'dart-web-v1' (rows of the older 'dart-v1/xml' extractor are re-extracted once); extract note like
      'web;overview;products'; raw_sha256 / raw_bytes of the fetched section HTML (not stored). A result missing a
      part for a temporary reason (failed products fetch, a truncated body) is stored under 'dart-web-v1/partial'
      (zip mode: 'dart-v1/xml/partial'), so the skip test fails and the next run fetches it again. The skip test
      compares the extractor, so switching between modes 'web' and 'zip' re-extracts every company once.
      Provenance: filing_batch snapshots of web documents record rate_key 'dart-web', auth 'none' and 'fetched_via'
      (list.json aux batches keep rate_key 'dart', auth 'query:<opendart-api-key>').
   Mode 'zip' (sync(mode='zip')) keeps the OpenDART path: document.xml?rcept_no=.. returns a ZIP of DART XML; the
   main document is <rcept_no>.xml. The section under the 'II. 사업의 내용' title starting at '1. 사업의 개요' is cut
   at the next numbered title (TOC titles, ATOC="Y", when the document marks them). Encodings: UTF-8 / EUC-KR
   (cp949) by declaration or detection. Extractor 'dart-v1/xml'.
5. JSON status codes: '000' ok; '013' no data -> 'no_annual_filing'; '020' quota exceeded and '101' improper access
   -> stop the run like http.Blocked (cooldown marker, command 'sync-dart'); '010'/'011'/'012'/'901' key / IP
   errors -> stop the run ('key_rejected'); '800' maintenance -> stop ('service_unavailable'); anything else is a
   per-company error. http.Blocked stops the run at once (marker before any DB write); 5 consecutive errors stop
   it; Ctrl-C / SIGTERM flush and record 'interrupted'. Rate: rate_key 'dart', >= 0.5 s between OpenDART requests
   (1 list.json per company in web mode, far below the ~20,000/day quota for a full KRX pass); the website has its
   own rate key 'dart-web' (>= 1 s) and budget lock 'dart-web' (taken before 'dart'; the CLI holds and reports Busy
   for 'dart' only, so a Busy 'dart-web' lock is a plain CLI error, before any request). http.Blocked on the
   website (403/429/challenge) stops the run like any block. WEB_REFUSAL_STOP (3) consecutive website refusals
   (main.do 3xx or without TOC, viewer.do 3xx or unverified; reset by a verified section) stop the run like a block
   (stopped_reason 'access_refused', cooldown marker); notes carry bytes / sha / page title / Location so the real
   refusal page can be identified on the first live run. SHORT_SECTION_STOP (5) consecutive 'section_too_short'
   results stop the run ('consecutive_errors').
6. Deadlines (http.Client total wall-clock time per attempt; live 2026-09-26 a document.xml download hung 10+ minutes
   at 0% CPU on an ESTABLISHED socket, the per-operation socket timeout never firing): list.json LIST_DEADLINE_S
   (60 s), document.xml DOC_DEADLINE_S (300 s), corpCode.xml CORPCODE_DEADLINE_S (900 s: ~3.6 MB at ~10 KB/s),
   website pages WEB_DEADLINE_S (60 s). A request that hits its deadline
   (http.RequestTimeout) is recorded crawl_state 'error' with note 'deadline;...' ('stall;...' for a socket-timeout
   stall mid-body) and the run goes on to the next company; only MAX_CONSECUTIVE_ERRORS consecutive errors stop it.

SEEN LIVE 2026-09-26 on a large issuer's 사업보고서 (2025.12); tests use synthetic equivalents
(tests/fixtures/dart_web_*_20260310009990.html, tools/synthetic_fixtures/dart_web.py):
the main.do TOC script (node1/node2/node3 objects, fields text/id/rcpNo/dcmNo/eleId/offset/length/dtd/tocNo/atocId)
and the viewer.do section HTML (UTF-8, <P class='section-2'> title, TABLE/TR/TD tables, <BR/> line breaks).

SPEC-DERIVED, NOT YET SEEN LIVE (no API key existed when this was written; validate on the first live run):
- list.json fields (status, message, total_count, list[{corp_code, corp_name, stock_code, corp_cls, report_nm,
  rcept_no, flr_nm, rcept_dt 'YYYYMMDD', rm}]) and newest-first default ordering (the code sorts anyway);
- report_nm shapes '사업보고서 (2024.12)' and '[기재정정]사업보고서 (2024.12)';
- the error envelope of the ZIP endpoints (corpCode.xml, document.xml): an XML <result><status>..</status>
  <message>..</message></result> (JSON accepted too) instead of a ZIP;
- the DART XML title markup <TITLE ATOC="Y" AASSOCNOTE="D-0-2-1-0">1. 사업의 개요</TITLE> inside
  'II. 사업의 내용', and cell tags TD/TH/TE/TU;
- that business reports filed before the 2020 format change also use a '1. 사업의 개요' title (if not, the whole
  'II. 사업의 내용' text is used, noted 'business_section_no_overview_title').
"""
from __future__ import annotations

import contextlib
import datetime as dt
import html as htmlmod
import http.client
import json
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote

from .. import guard, store
from ..config import Config
from ..http import Blocked, RequestTimeout
from .deep_sections import DEEP_SECTION
from .edinet import (FINAL_WAIT_S, ApiKeyMissing, ApiStop, SyncRun, date_or_none, deadline_note, decode_bytes,
                     err_note, filter_lines, is_zip, key_redactor, load_lines, normalise_text, prepare_client,
                     read_zip, short_description_cjk, sigterm_as_interrupt, split_sid, stale_flag, stored_documents,
                     with_key)

SOURCE_ID = "dart_business_report"
API_BASE = "https://opendart.fss.or.kr/api"
CORPCODE_URL = API_BASE + "/corpCode.xml"
LIST_URL = (API_BASE + "/list.json?corp_code={corp_code}&bgn_de={bgn_de}&end_de={end_de}"
            "&pblntf_ty=A&pblntf_detail_ty=A001&page_no=1&page_count=100")
DOC_URL = API_BASE + "/document.xml?rcept_no={rcept_no}"
VIEWER_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"      # also the web mode's TOC page
WEB_MAIN_URL = VIEWER_URL
WEB_SECTION_URL = "https://dart.fss.or.kr/report/viewer.do"
KEY_PARAM = "crtfc_key"
KEY_PLACEHOLDER = "<opendart-api-key>"
KEY_FILE = "opendart_api_key"
KEY_ENV = "JEVSCREEN_OPENDART_API_KEY"
RATE_KEY = "dart"
DEFAULT_MIN_INTERVAL_S = 0.5
ID_TYPE = "dart_corp_code"
COMMAND = "sync-dart"
EXTRACTOR_VERSION = "dart-v1"            # mode 'zip': extractor 'dart-v1/xml'
WEB_EXTRACTOR_VERSION = "dart-web-v1"    # mode 'web'
# v2 = v1 + the deep appendix ('매출 및 수주상황', 'IV. 이사의 경영진단 및 분석의견'; sources/deep_sections.py), written
# only by an on-demand deep fetch (sync(deep=True) with codes) into its own row and text file (section
# deep_sections.DEEP_SECTION); a plain sync never reads, settles on or replaces a deep row.
DEEP_WEB_EXTRACTOR_VERSION = "dart-web-v2"
MODES = ("web", "zip")
WEB_RATE_KEY = "dart-web"                # dart.fss.or.kr: own limiter key and budget lock
WEB_MIN_INTERVAL_S = 1.0                 # >= 1 s between website request starts
PRODUCTS_HEADING = "【주요 제품 및 서비스】"
FORM_LABEL = "사업보고서"
SECTION = "business"
KR_VENUES = frozenset({"KRX", "KOSDAQ", "KONEX"})
KST = dt.timezone(dt.timedelta(hours=9))
LOOKBACK_DAYS = 800          # two annual cycles: a report for the latest fiscal year plus a margin

STATUS_OK = "000"
STATUS_NO_DATA = "013"
STATUS_NO_FILE = "014"
STOP_STATUSES: dict[str, tuple[str, bool]] = {   # status -> (stopped_reason, cooldown marker)
    "020": ("quota_exceeded", True),
    "101": ("access_refused", True),
    "010": ("key_rejected", False),
    "011": ("key_rejected", False),
    "012": ("key_rejected", False),
    "901": ("key_rejected", False),
    "800": ("service_unavailable", False),
}

MIN_SECTION_CHARS = 80
MAX_SECTION_CHARS = 300_000
MAX_DOC_BYTES = 64 * 1024 * 1024
MAX_CORPCODE_BYTES = 64 * 1024 * 1024
MAX_WEB_BYTES = 16 * 1024 * 1024
PRODUCTS_MAX_CHARS = 8_000      # '주요 제품 및 서비스' appended to '사업의 개요'
CHAPTER_MAX_CHARS = 20_000      # 'fallback:chapter': the whole 'II. 사업의 내용' text
LIST_DEADLINE_S = 60.0          # list.json
DOC_DEADLINE_S = 300.0          # document.xml (ZIP of the whole report; mode 'zip')
CORPCODE_DEADLINE_S = 900.0     # corpCode.xml (ZIP of every corp code, ~3.6 MB at ~10 KB/s measured 2026-09-26)
WEB_DEADLINE_S = 60.0           # main.do / viewer.do (112 KB in 2.0 s, 4.4 KB in 0.9 s measured 2026-09-26)
CORPCODE_REUSE_DAYS = 7         # reuse a raw corpCode zip saved by an earlier run at most this many days ago
CORPCODE_MIN_ROWS = 50_000      # a sane corpCode.xml (live 2026-09-26: 119,447 corp codes, 3,994 with a stock code);
CORPCODE_MIN_STOCK_CODES = 2_000   # below either -> 'corpcode_suspect': not used, nothing deleted
PARTIAL_SUFFIX = "/partial"     # extractor suffix of a result missing a part for a temporary reason: redone next run
WEB_REFUSAL_STOP = 3            # consecutive website refusals (no TOC, 3xx, unverified section) -> stop + cooldown
SHORT_SECTION_STOP = 5          # consecutive 'section_too_short' results -> stop the run ('consecutive_errors')
WEB_ORIGIN = "https://dart.fss.or.kr/"

CRAWL_STATUSES = ("ok", "no_annual_filing", "extract_failed", "error", "blocked")


class DartApiKeyMissing(ApiKeyMissing):
    """No OpenDART crtfc_key (env JEVSCREEN_OPENDART_API_KEY or <home>/opendart_api_key)."""


class DartStatusError(RuntimeError):
    """A non-OK OpenDART status that concerns only this request."""

    def __init__(self, status: str, message: str | None = None):
        super().__init__(f"opendart status {status}: {message or ''}".strip())
        self.status, self.message = status, message


def raise_for_status(status: str | None, message: str | None = None) -> None:
    """'000' (or no status) passes; run-level statuses raise ApiStop; others raise DartStatusError."""
    status = (status or "").strip()
    if status in ("", STATUS_OK):
        return
    if status in STOP_STATUSES:
        reason, cooldown = STOP_STATUSES[status]
        raise ApiStop(reason, status, message=message, cooldown=cooldown)
    raise DartStatusError(status, message)


def envelope_status(body: bytes) -> tuple[str | None, str | None]:
    """(status, message) of a non-ZIP answer of a ZIP endpoint: XML <result><status> or JSON {status, message}."""
    head = (body or b"")[:1 << 16]
    try:
        payload = json.loads(head)
        if isinstance(payload, Mapping):
            s = payload.get("status")
            return (str(s).strip() if s is not None else None), payload.get("message")
    except ValueError:
        pass
    text, _ = decode_bytes(head, prefer=("utf-8", "cp949"))
    m = re.search(r"<status>\s*([^<]+?)\s*</status>", text, re.I)
    msg = re.search(r"<message>\s*([^<]+?)\s*</message>", text, re.I)
    return (m.group(1).strip() if m else None), (msg.group(1).strip() if msg else None)


# ============================================================================================ corp codes


def parse_corp_codes(data: bytes | str) -> list[dict]:
    """corpCode.xml answer (ZIP holding CORPCODE.xml, or the XML itself) ->
    [{corp_code, corp_name, corp_eng_name, stock_code (None when blank), modify_date}].
    A non-ZIP error envelope raises ApiStop / DartStatusError."""
    if isinstance(data, (bytes, bytearray)):
        data = bytes(data)
        if is_zip(data):
            members = [(n, b) for n, b in read_zip(data) if n.lower().endswith(".xml")]
            if not members:
                raise ValueError("corpcode_zip_without_xml")
            members.sort(key=lambda nb: (0 if "corpcode" in nb[0].lower() else 1, -len(nb[1])))
            xml_bytes = members[0][1]
        else:
            status, msg = envelope_status(data)
            if status and status != STATUS_OK:
                raise_for_status(status, msg)
            xml_bytes = data
    else:
        xml_bytes = data.encode("utf-8")
    out: list[dict] = []
    try:
        root = ET.fromstring(xml_bytes)
        for el in root.iter("list"):
            out.append({k: (el.findtext(k) or "").strip() or None
                        for k in ("corp_code", "corp_name", "corp_eng_name", "stock_code", "modify_date")})
    except ET.ParseError:
        text, _ = decode_bytes(xml_bytes, prefer=("utf-8", "cp949"))
        for block in re.findall(r"<list>(.*?)</list>", text, re.S | re.I):
            row = {}
            for k in ("corp_code", "corp_name", "corp_eng_name", "stock_code", "modify_date"):
                m = re.search(rf"<{k}>(.*?)</{k}>", block, re.S | re.I)
                v = htmlmod.unescape(re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", m.group(1), flags=re.S)).strip() \
                    if m else ""
                row[k] = v or None
            out.append(row)
    return [r for r in out if r.get("corp_code")]


def map_securities_to_corp(securities: Sequence[Mapping[str, Any]], corp_codes: Sequence[Mapping[str, Any]], *,
                           report: dict | None = None) -> list[tuple[str, str, str]]:
    """Map TradingView lines on KRX venues (6-character codes) to DART corp_codes by stock_code.
    Returns [(security_id, corp_code, 'stock_code')]; `report` receives considered, non_kr, unmatched, ambiguous."""
    index: dict[str, set[str]] = {}
    for e in corp_codes:
        sc = (e.get("stock_code") or "").strip().upper()
        if re.fullmatch(r"[0-9A-Z]{6}", sc):
            index.setdefault(sc, set()).add(str(e["corp_code"]).strip())
    rep = {"considered": 0, "non_kr": 0, "unmatched": 0, "ambiguous": []}
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for sec in securities:
        sid = str(sec.get("security_id") or "")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        exch, sym = split_sid(sec)
        if exch not in KR_VENUES or not sym:
            rep["non_kr"] += 1
            continue
        rep["considered"] += 1
        sym = unicodedata.normalize("NFKC", sym).upper()
        cands = index.get(sym) if re.fullmatch(r"[0-9A-Z]{6}", sym) else None
        if not cands:
            rep["unmatched"] += 1
            continue
        if len(cands) > 1:
            rep["ambiguous"].append({"security_id": sid, "stock_code": sym, "corp_codes": sorted(cands)})
            continue
        out.append((sid, next(iter(cands)), "stock_code"))
    if report is not None:
        report.update(rep)
    return out


def _utc_today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def corpcode_problem(rows: Sequence[Mapping[str, Any]]) -> str | None:
    """'corpcode_suspect:rows=<n>;stock_codes=<n>' when a parsed corpCode.xml is too small to be the full list
    (a partial or wrong file that still parses would otherwise drop the dart_corp_code identifiers of every KRX line
    it misses), else None."""
    n = len(rows)
    s = sum(1 for r in rows if re.fullmatch(r"[0-9A-Z]{6}", str(r.get("stock_code") or "").strip().upper()))
    if n < CORPCODE_MIN_ROWS or s < CORPCODE_MIN_STOCK_CODES:
        return f"corpcode_suspect:rows={n};stock_codes={s}"
    return None


def find_cached_corpcode(cfg: Config, *, max_age_days: int = CORPCODE_REUSE_DAYS,
                         today: dt.date | None = None) -> tuple[Path, bytes, list[dict]] | None:
    """The newest raw corpCode zip an earlier run saved with store.save_raw
    (<raw_dir>/dart_business_report/<YYYY-MM-DD>/dart_corpcode-*.zip) whose day folder is at most `max_age_days`
    old (UTC; one day in the future tolerated for clock skew) and that parses to a sane list (corpcode_problem()
    None): (path, bytes, parsed rows), else None. Unreadable / corrupt / non-ZIP / suspect copies are skipped (an
    older fresh one may still be used). Read-only."""
    root = Path(cfg.raw_dir) / SOURCE_ID
    today = today or _utc_today()
    cands: list[tuple[dt.date, float, str]] = []
    try:
        paths = list(root.glob("*/dart_corpcode-*.zip"))
    except OSError:
        return None
    for p in paths:
        try:
            day = dt.date.fromisoformat(p.parent.name)
        except ValueError:
            continue
        if not -1 <= (today - day).days <= max_age_days:
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        cands.append((day, mtime, str(p)))
    for _, _, name in sorted(cands, reverse=True):
        p = Path(name)
        try:
            data = p.read_bytes()
            if not is_zip(data):
                continue
            rows = parse_corp_codes(data)
        except Exception:      # noqa: BLE001 - a cache probe: any unreadable copy is just not reused
            continue
        if rows and corpcode_problem(rows) is None:
            return p, data, rows
    return None


# ============================================================================================ filing selection

_PERIOD_RE = re.compile(r"\((\d{4})\s*[.\-/]\s*(\d{1,2})\)")
_PREFIX_RE = re.compile(r"^\s*\[([^\]]*)\]\s*")


def _period_end(report_nm: str) -> dt.date | None:
    m = _PERIOD_RE.search(report_nm)
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    if not 1 <= mo <= 12:
        return None
    nxt = dt.date(y + (mo == 12), mo % 12 + 1, 1)
    return nxt - dt.timedelta(days=1)


def select_business_report(rows: Iterable[Mapping[str, Any]], *, as_of: dt.date | None = None) -> dict | None:
    """Newest 사업보고서 among list.json rows: the newest period wins; within it the original beats corrections
    ('[기재정정]' etc.), then the newest receipt. Returns {rcept_no, report_nm, rcept_dt, period_end, amendment,
    flags} or None. flags: 'corrected_later' (a correction of the chosen period exists, or rm contains '정'),
    'amendment:<label>' when only a correction exists, 'stale:<years>'."""
    cands = []
    for r in rows:
        name = unicodedata.normalize("NFKC", str(r.get("report_nm") or "")).strip()
        rcept_no = str(r.get("rcept_no") or "").strip()
        if "사업보고서" not in name or not re.fullmatch(r"\d{14}", rcept_no):
            continue
        m = _PREFIX_RE.match(name)
        label = m.group(1).strip() if m else None
        cands.append({"rcept_no": rcept_no, "report_nm": name, "rcept_dt": str(r.get("rcept_dt") or "").strip(),
                      "period_end": _period_end(name), "amendment": bool(label), "label": label,
                      "rm": str(r.get("rm") or "")})
    if not cands:
        return None
    newest_period = max((c["period_end"] for c in cands if c["period_end"]), default=None)
    pool = [c for c in cands if c["period_end"] == newest_period] if newest_period else cands
    best = max(pool, key=lambda c: (not c["amendment"], c["rcept_dt"], c["rcept_no"]))
    flags: list[str] = []
    if best["amendment"]:
        flags.append(f"amendment:{best['label']}")
    elif any(c["amendment"] for c in pool) or "정" in best["rm"]:
        flags.append("corrected_later")
    today = as_of or dt.datetime.now(KST).date()
    st = stale_flag(best["period_end"] or date_or_none(best["rcept_dt"]), today)
    if st:
        flags.append(st)
    return {k: best[k] for k in ("rcept_no", "report_nm", "rcept_dt", "period_end", "amendment")} | {"flags": flags}


# ============================================================================================ DART XML -> section

_TITLE_RE = re.compile(r"<TITLE\b([^>]*)>(.*?)</TITLE\s*>", re.S | re.I)
_ATOC_Y_RE = re.compile(r"""\bATOC\s*=\s*["']?Y""", re.I)
_ROMAN_RE = re.compile(r"^(?:I|II|III|IV|V|VI|VII|VIII|IX|X|XI|XII|XIII|XIV)\s*[.．]")
_BIZ_RE = re.compile(r"^II\s*[.．]\s*사업의\s*내용")
_OVERVIEW_RE = re.compile(r"^1\s*[.．)]\s*(?:\([^)]*\)\s*)?사업의\s*개요")
_NUMBERED_RE = re.compile(r"^\d{1,2}\s*[.．)]")
_BLOCK_TAG_RE = re.compile(r"</?(?:P|TR|TITLE|TABLE|TBODY|THEAD|BR|DIV|LI|H\d|PGBRK|LIBRARY|SECTION-\d+|"
                           r"TABLE-GROUP|CORRECTION|PART)\b[^>]*>", re.I)
_CELL_TAG_RE = re.compile(r"</?(?:TD|TH|TE|TU)\b[^>]*>", re.I)
_DROP_RE = re.compile(r"<(IMG|IMAGE-FILE|STYLE|SCRIPT)\b[^>]*>.*?</\1\s*>|<!--.*?-->", re.S | re.I)


def _title_text(raw: str) -> str:
    t = htmlmod.unescape(re.sub(r"<[^>]+>", "", raw))
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", t)).strip()


_CR_ENTITY_RE = re.compile(r"&cr;", re.I)


def dart_xml_to_text(fragment: str) -> str:
    """DART XML fragment -> plain text: block tags -> line breaks, table cells -> spaces, images dropped,
    other tags removed, DART's custom '&cr;' line-break entity turned into a line break (html.unescape leaves it;
    seen in DSD cells, to confirm on the first live run), entities unescaped once, whitespace normalised."""
    s = _CR_ENTITY_RE.sub("\n", fragment)
    s = _DROP_RE.sub(" ", s)
    s = _BLOCK_TAG_RE.sub("\n", s)
    s = _CELL_TAG_RE.sub(" ", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = htmlmod.unescape(s)
    return normalise_text(s)


def extract_overview(xml_text: str) -> tuple[str | None, str]:
    """Main DART XML document -> ('1. 사업의 개요' text or None, note).

    Titles are the TOC titles (ATOC="Y") when the document marks any, else all TITLE elements. The section runs
    from the '1. 사업의 개요' title inside 'II. 사업의 내용' to the next numbered or top-level (roman) title.
    Fallbacks (noted): the whole 'II. 사업의 내용' region when it has no overview title; an overview title outside
    such a region."""
    titles = [(m.start(), m.end(), _title_text(m.group(2)), bool(_ATOC_Y_RE.search(m.group(1))))
              for m in _TITLE_RE.finditer(xml_text)]
    if any(t[3] for t in titles):
        titles = [t for t in titles if t[3]]
    n = len(titles)
    biz = next((i for i in range(n) if _BIZ_RE.match(titles[i][2])), None)

    def next_boundary(after: int, limit: int, numbered: bool) -> int:
        for j in range(after + 1, n):
            if titles[j][0] >= limit:
                break
            t = titles[j][2]
            if _ROMAN_RE.match(t) or (numbered and _NUMBERED_RE.match(t)):
                return titles[j][0]
        return limit

    if biz is not None:
        region_end = next_boundary(biz, len(xml_text), numbered=False)
        ov = next((j for j in range(biz + 1, n) if titles[j][0] < region_end and _OVERVIEW_RE.match(titles[j][2])),
                  None)
        if ov is not None:
            frag, note = xml_text[titles[ov][1]:next_boundary(ov, region_end, numbered=True)], "overview"
        else:
            frag, note = xml_text[titles[biz][1]:region_end], "business_section_no_overview_title"
    else:
        ov = next((j for j in range(n) if _OVERVIEW_RE.match(titles[j][2])), None)
        if ov is None:
            return None, "business_section_not_found"
        frag, note = xml_text[titles[ov][1]:next_boundary(ov, len(xml_text), numbered=True)], \
            "overview_without_business_title"
    text = dart_xml_to_text(frag)
    if len(text) < MIN_SECTION_CHARS:
        return None, f"section_too_short:{len(text)};{note}"
    if len(text) > MAX_SECTION_CHARS:
        text, note = text[:MAX_SECTION_CHARS], note + ";capped"
    return text, note


def main_document(members: Sequence[tuple[str, bytes]], rcept_no: str) -> tuple[str, bytes] | None:
    """<rcept_no>.xml, else the largest .xml member (attachments are <rcept_no>_NNNNN.xml)."""
    xmls = [(n, b) for n, b in members if n.lower().endswith(".xml")]
    exact = next(((n, b) for n, b in xmls if Path(n).name == f"{rcept_no}.xml"), None)
    if exact:
        return exact
    return max(xmls, key=lambda nb: len(nb[1])) if xmls else None


# ============================================================================================ DART website -> section

_TOC_DECL_RE = re.compile(r"\b(?:var\s+|let\s+|const\s+)?node(\d{1,2})\s*=\s*\{\s*\}")
_TOC_FIELD_RE = re.compile(
    r"""\bnode(\d{1,2})\s*(?:\[\s*['"](\w+)['"]\s*\]|\.(\w+))\s*=\s*"""
    r"""(?:"((?:[^"\\\r\n]|\\.)*)"|'((?:[^'\\\r\n]|\\.)*)'|(-?\d+)\b)""")
_TOC_STMT_RE = re.compile(f"{_TOC_DECL_RE.pattern}|{_TOC_FIELD_RE.pattern}")
_JS_ESC_RE = re.compile(r"\\(u[0-9A-Fa-f]{4}|x[0-9A-Fa-f]{2}|.)", re.S)
TOC_FIELDS = ("text", "id", "rcpNo", "dcmNo", "eleId", "offset", "length", "dtd", "tocNo", "atocId")


def _js_unescape(s: str) -> str:
    def rep(m: re.Match) -> str:
        e = m.group(1)
        if e[0] in "ux" and len(e) > 1:
            return chr(int(e[1:], 16))
        return {"n": "\n", "t": "\t", "r": "", "b": "", "f": "", "v": "", "0": ""}.get(e, e)
    return _JS_ESC_RE.sub(rep, s)


def _toc_script(html: str) -> str:
    """The makeToc() function body when present (other scripts could reuse 'nodeN' names), else the whole page."""
    m = re.search(r"function\s+makeToc\s*\(", html)
    if not m:
        return html
    end = re.search(r"\n\s*function\s+\w+\s*\(", html[m.end():])
    return html[m.start():m.end() + end.start()] if end else html[m.start():]


def parse_toc(html: str) -> list[dict]:
    """main.do page -> TOC nodes in page order: [{'text', 'dcmNo', 'eleId', 'offset', 'length', 'dtd', 'rcpNo', ...
    (string values as given), 'level' (N of nodeN), 'parent' (index or None), 'children' (indexes), 'index'}].

    Statement-driven, so field order does not matter: `var nodeN = {}` opens a node at level N whose parent is the
    newest node of a lower level; `nodeN['field'] = "value"` (or nodeN.field, single quotes, bare numbers) sets a
    field of the newest level-N node (one is opened when a field comes before any declaration). JS escapes and HTML
    entities in values are decoded. Nodes without a text are dropped (their children move up)."""
    script = _toc_script(html)
    nodes: list[dict] = []
    current: dict[int, int] = {}          # level -> index of the newest node at that level

    def open_node(level: int) -> int:
        parent = next((current[lv] for lv in sorted(current, reverse=True) if lv < level), None)
        nodes.append({"level": level, "parent": parent, "children": [], "index": len(nodes)})
        idx = len(nodes) - 1
        current[level] = idx
        for lv in [lv for lv in current if lv > level]:
            del current[lv]
        return idx

    for m in _TOC_STMT_RE.finditer(script):
        if m.group(1) is not None:
            open_node(int(m.group(1)))
            continue
        level = int(m.group(2))
        field = m.group(3) or m.group(4)
        raw = next((g for g in (m.group(5), m.group(6), m.group(7)) if g is not None), "")
        idx = current.get(level)
        if idx is None:
            idx = open_node(level)
        value = htmlmod.unescape(_js_unescape(raw)).strip()
        if field == "text":
            value = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()
        nodes[idx][field] = value
    # drop text-less nodes, re-link parents, rebuild children lists
    keep = [n for n in nodes if n.get("text")]
    new_index = {}
    for i, n in enumerate(keep):
        new_index[n["index"]] = i
    by_old = {n["index"]: n for n in nodes}
    out: list[dict] = []
    for i, n in enumerate(keep):
        parent = n["parent"]
        while parent is not None and parent not in new_index:
            parent = by_old[parent]["parent"]
        out.append({**n, "index": i, "parent": new_index.get(parent) if parent is not None else None,
                    "children": []})
    for n in out:
        if n["parent"] is not None:
            out[n["parent"]]["children"].append(n["index"])
    return out


_TITLE_NUM_RE = re.compile(
    r"^(?:(?:[IVX]{1,5}|\d{1,2}(?:-\d{1,2})*)(?:\s*[.．)]|\s+)|[가-힣]\s*[.．)]|\(\s*(?:\d{1,2}|[가-힣])\s*\))\s*")
_TITLE_PAREN_RE = re.compile(r"^[(\[【〔][^)\]】〕]{1,20}[)\]】〕]\s*")


def title_key(title: str | None) -> str:
    """TOC / section title -> comparison key: NFKC ('Ⅱ' -> 'II'), numbering ('1.', '1)', '1 ', '가.', 'II.', 'II ',
    '2-1.', '(1)', '①') and a parenthesised prefix ('(금융업)') removed, all whitespace removed."""
    t = unicodedata.normalize("NFKC", re.sub(r"^\s*[①-⑳]\s*", "", title or "")).strip()   # NFKC: '①' -> '1'
    for _ in range(3):
        t2 = _TITLE_PAREN_RE.sub("", _TITLE_NUM_RE.sub("", t)).strip()
        if t2 == t:
            break
        t = t2
    return re.sub(r"\s+", "", t)


def _is_chapter(key: str) -> bool:
    return key == "사업의내용"


def _is_overview(key: str) -> bool:
    return key.startswith("사업의개요")


def _is_products(key: str) -> bool:
    return key.startswith("주요제품")      # '주요 제품 및 서비스', '주요 제품, 서비스 등', '주요 제품 및 원재료 등'


def section_params(node: Mapping[str, Any], rcept_no: str | None = None) -> dict[str, str] | None:
    """viewer.do query parameters of a TOC node, or None when it has no usable range (dcmNo, offset, length)."""
    dcm, off, ln = (str(node.get(k) or "").strip() for k in ("dcmNo", "offset", "length"))
    if not (re.fullmatch(r"\d+", dcm) and re.fullmatch(r"\d+", off) and re.fullmatch(r"\d+", ln)) or int(ln) <= 0:
        return None
    rcp = str(node.get("rcpNo") or "").strip()
    if not re.fullmatch(r"\d{14}", rcp):
        rcp = rcept_no or ""
    params = {"rcpNo": rcp, "dcmNo": dcm}
    ele = str(node.get("eleId") or "").strip()
    if ele:
        params["eleId"] = ele
    params.update(offset=off, length=ln, dtd=str(node.get("dtd") or "").strip() or "dart4.xsd")
    return params


def section_url(node: Mapping[str, Any], rcept_no: str | None = None) -> str | None:
    params = section_params(node, rcept_no)
    if params is None:
        return None
    return WEB_SECTION_URL + "?" + "&".join(f"{k}={quote(v, safe='.')}" for k, v in params.items())


def find_business_sections(nodes: Sequence[Mapping[str, Any]]) -> dict:
    """TOC nodes -> {'chapter', 'overview', 'products': node or None, 'notes': [...]}.

    The chapter is the first '사업의 내용' node (top level preferred); '사업의 개요' and '주요 제품 ...' are searched in
    its subtree (page order, any depth) and must have a range; a products node inside the overview's own subtree
    is ignored (its text is part of the overview). Without such a chapter the whole TOC is searched (note
    'overview_without_business_title'). The chapter is returned only when it has a range (for 'fallback:chapter').
    Only the first overview / products item is used; further ones (a mixed-business company's '1. (제조업) 사업의
    개요' and '(금융업) 사업의 개요', not nested in the first) are noted 'multiple_overview:<n>' /
    'multiple_products:<n>' (n = candidates found)."""
    notes: list[str] = []
    chapters = [n for n in nodes if _is_chapter(title_key(n.get("text")))]
    chapter = min(chapters, key=lambda n: (n["level"], n["index"])) if chapters else None

    def subtree(root: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        out, stack = [], list(reversed(root["children"]))
        while stack:
            n = nodes[stack.pop()]
            out.append(n)
            stack.extend(reversed(n["children"]))
        return out

    pool = subtree(chapter) if chapter is not None else list(nodes)
    candidates = [n for n in pool if _is_overview(title_key(n.get("text"))) and section_params(n)]
    overview = candidates[0] if candidates else None
    inside = {n["index"] for n in subtree(overview)} if overview is not None else set()
    overviews = [n for n in candidates if n["index"] not in inside]   # not nested in the first one
    products_all = [n for n in pool if _is_products(title_key(n.get("text"))) and n["index"] not in inside
                    and section_params(n)]
    products = products_all[0] if products_all else None
    if chapter is None and overview is not None:
        notes.append("overview_without_business_title")
    if len(overviews) > 1:
        notes.append(f"multiple_overview:{len(overviews)}")
    if len(products_all) > 1:
        notes.append(f"multiple_products:{len(products_all)}")
    return {"chapter": chapter if chapter is not None and section_params(chapter) else None,
            "overview": overview, "products": products, "notes": notes}


_H_DROP_RE = re.compile(r"<(script|style|head|title|noscript)\b[^>]*>.*?</\1\s*>|<!--.*?-->", re.S | re.I)
_H_TABLE_RE = re.compile(r"<table\b[^>]*>((?:(?!<table\b).)*?)</table\s*>", re.S | re.I)   # innermost tables
_H_ROW_SPLIT_RE = re.compile(r"<tr\b[^>]*>", re.I)
_H_CELL_SPLIT_RE = re.compile(r"<t[dh]\b[^>]*>", re.I)
_H_CELL_END_RE = re.compile(r"</t[dhr]\s*>|</t(?:body|head|foot)\s*>", re.I)
_H_BR_RE = re.compile(r"<br\b[^>]*>", re.I)
_H_BLOCK_RE = re.compile(r"</?(?:p|div|h[1-6]|li|ul|ol|dl|dt|dd|blockquote|section|article|center|body|pre)\b[^>]*>",
                         re.I)
_H_TAG_RE = re.compile(r"<[^>]+>")
_H_CELL_BLOCK_RE = re.compile(r"</?(?:p|div|h[1-6]|li|ul|ol|dl|dt|dd|blockquote|section|article|center|pre|tr|table|"
                              r"tbody|thead|tfoot)\b[^>]*>", re.I)
_H_SECTION_TITLE_RE = re.compile(r"<p\b[^>]*\bclass\s*=\s*[\"']?section-\d+\b[^>]*>(.*?)</p\s*>", re.S | re.I)
_H_SECTION_CLASS_RE = re.compile(r"\bclass\s*=\s*[\"']?section-\d+\b", re.I)


def _cell_text(fragment: str) -> str:
    s = _H_BR_RE.sub(" ", fragment)
    s = _H_CELL_BLOCK_RE.sub(" ", s)          # '<p>a</p><p>b</p>' in one cell -> 'a b', not 'ab'
    s = _H_TAG_RE.sub("", s)
    return re.sub(r"\s+", " ", htmlmod.unescape(s).replace("\xa0", " ")).strip()


def _table_lines(inner: str) -> str:
    lines = []
    for row in _H_ROW_SPLIT_RE.split(inner)[1:]:
        cells = [_cell_text(_H_CELL_END_RE.split(c, 1)[0]) for c in _H_CELL_SPLIT_RE.split(row)[1:]]
        cells = [c for c in cells if c]
        if cells:
            lines.append(" | ".join(cells))
    # escape back so the single html.unescape of the whole page leaves the cell text as it is
    return "\n\n" + "\n".join(htmlmod.escape(ln, quote=False) for ln in lines) + "\n\n"


def dart_html_to_text(html: str) -> str:
    """viewer.do section HTML -> plain text: head / scripts / comments dropped; each table row one line (cells
    joined by ' | ', empty cells skipped, <BR> inside a cell a space); <BR> a line break; P / DIV / headings a
    paragraph break; other tags removed; entities unescaped once; whitespace normalised (blank lines collapsed).
    Nested tables are resolved inside-out: an inner table's rows end up as one cell of the outer row."""
    s = _H_DROP_RE.sub(" ", html)
    for _ in range(32):                        # innermost tables first; a table nested deeper than 32 stays tags
        s, n = _H_TABLE_RE.subn(lambda m: _table_lines(m.group(1)), s)
        if not n:
            break
    s = _H_BR_RE.sub("\n", s)
    s = _H_BLOCK_RE.sub("\n\n", s)
    s = _H_TAG_RE.sub("", s)
    s = htmlmod.unescape(s)
    return normalise_text(s)


def _leading_title(html: str, key: str) -> re.Match | None:
    """The first <P class='section-N'> element when nothing but markup precedes it and its text has title key
    `key` (a title broken by <BR>, '1. 사업의<BR/>개요', included)."""
    m = _H_SECTION_TITLE_RE.search(html)
    if not m or title_key(_title_text(m.group(1))) != key:
        return None
    before = _H_TAG_RE.sub("", _H_DROP_RE.sub(" ", html[:m.start()]))
    return m if not htmlmod.unescape(before).replace("\xa0", " ").strip() else None


def section_text(html: str, title: str | None = None) -> str:
    """dart_html_to_text() without the leading title (the section's own TOC title, e.g. '1. 사업의 개요'): the
    leading <P class='section-N'> title element is removed before conversion, then leading lines whose title key
    (alone, or the first 2-3 non-empty lines joined) equals the title's are dropped."""
    key = title_key(title) if title else ""
    if key:
        m = _leading_title(html, key)
        if m:
            html = html[:m.start()] + html[m.end():]
    text = dart_html_to_text(html)
    if not key:
        return text
    lines = text.split("\n")
    while True:
        filled = [i for i, ln in enumerate(lines) if ln.strip()][:3]
        k = next((k for k in (1, 2, 3) if len(filled) >= k
                  and title_key("".join(lines[i] for i in filled[:k])) == key), None)
        if k is None:
            break
        lines = lines[filled[k - 1] + 1:]
    return "\n".join(lines).strip()


def section_verified(html: str, title: str | None = None) -> bool:
    """True when a viewer.do answer looks like a report section: it has a <P class='section-N'> element (seen on
    every live section page), or one of its first three non-empty text lines has the node title's key. An HTTP 200
    error or throttling page (no Cloudflare signature, so http.Client does not raise Blocked) fails this check."""
    if _H_SECTION_CLASS_RE.search(html):
        return True
    key = title_key(title) if title else ""
    if not key:
        return False
    head = [ln for ln in dart_html_to_text(html[:200_000]).split("\n") if ln.strip()][:3]
    return any(title_key(ln) == key for ln in head) or title_key("".join(head[:2])) == key


class WebPacer:
    """>= min_interval_s between the STARTS of website requests (the shared http.Client limiter only knows its own
    min_interval_s, 0.5 s for OpenDART). clock / sleep are injectable for tests."""

    def __init__(self, min_interval_s: float, clock: Callable[[], float] | None = None,
                 sleep: Callable[[float], None] | None = None) -> None:
        self.min_interval_s, self._clock, self._sleep = float(min_interval_s), clock, sleep
        self.last: float | None = None

    def wait(self) -> None:
        clock, sleep = self._clock or time.monotonic, self._sleep or time.sleep
        if self.last is not None:
            gap = self.min_interval_s - (clock() - self.last)
            if gap > 0:
                sleep(gap)
        self.last = clock()


def text_path_for(cfg: Config, corp_code: str, rcept_no: str, section: str = SECTION) -> Path:
    return Path(cfg.home) / "docs" / "dart" / corp_code / f"{rcept_no}-{section}.txt"


# ============================================================================================ sync


def missing_key_message(cfg: Config) -> str:
    return ("OpenDART API key missing: register for a free OpenDART crtfc_key (https://opendart.fss.or.kr/), "
            f"then put it in {Path(cfg.home) / KEY_FILE} (data/{KEY_FILE}, git-ignored) or set {KEY_ENV}.")


def sync(cfg: Config, client: Any = None, *, limit: int | None = None, codes: Iterable[str] | str | None = None,
         refresh: bool = False, min_mcap_usd: float | None = None, lookback_days: int = LOOKBACK_DAYS,
         batch_size: int = 25, progress_every: int = 100, only_universe: bool = True,
         as_of: dt.date | None = None, mode: str = "web", reuse_corpcode: bool = True,
         on_company: Callable[[list[str], str, str | None], None] | None = None, deep: bool = False) -> dict:
    """Map KRX universe lines to DART corp codes and pull each company's latest 사업보고서 business section.

    Steps: (1) require cfg.opendart_api_key() (DartApiKeyMissing before any request); (2) reuse a raw corpCode zip
    saved <= CORPCODE_REUSE_DAYS ago (unless reuse_corpcode=False), else GET corpCode.xml and save it raw; snapshot,
    upsert identifiers ('dart_corp_code'), drop stale ones; (3) queue distinct corp codes by market cap desc
    (`codes` / `min_mcap_usd` / `limit` narrow it); (4) per company GET list.json, pick the report, skip when the
    stored document has the same rcept_no and extractor (unless refresh), then mode 'web' (default): main.do TOC +
    viewer.do '사업의 개요' and '주요 제품 및 서비스'; mode 'zip': document.xml + '1. 사업의 개요'; write the text file;
    per batch one short session. Returns a summary whose 'status' is ok | blocked | stopped_errors | interrupted |
    store_locked | error. on_company(security_ids, status, note): per-company events (SyncRun.on_company).
    deep=True (on-demand only: mode 'web' and `codes` required; jevscreen.topn_fetch): after the overview and
    products, the deep sections of the same report are fetched too (deep_sections.dart_deep_nodes, one viewer.do
    page each) and appended (extractor DEEP_WEB_EXTRACTOR_VERSION); a stored shallow text is read again once."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
    if deep and (mode != "web" or not codes):
        raise ValueError("deep=True needs mode 'web' and codes (an on-demand fetch of named companies)")
    key = cfg.opendart_api_key()
    if not key:
        raise DartApiKeyMissing(missing_key_message(cfg))
    redact = key_redactor(key, KEY_PLACEHOLDER)
    client = prepare_client(cfg, client, DEFAULT_MIN_INTERVAL_S)
    with contextlib.ExitStack() as stack:
        # 'dart-web' first: guard.Busy on it is raised before anything else is held or requested. The CLI holds
        # (and reports Busy for) 'dart' only, so a Busy 'dart-web' lock surfaces there as a plain error (exit 1,
        # runs row 'error'), still before any request.
        if mode == "web":
            stack.enter_context(guard.budget_lock(cfg, WEB_RATE_KEY, reentrant=True))
        stack.enter_context(guard.budget_lock(cfg, RATE_KEY, reentrant=True))
        stack.enter_context(sigterm_as_interrupt())
        return _sync(cfg, client, key, redact, limit=limit, codes=codes, refresh=refresh, min_mcap_usd=min_mcap_usd,
                     lookback_days=lookback_days, batch_size=batch_size, progress_every=progress_every,
                     only_universe=only_universe, as_of=as_of, mode=mode, reuse_corpcode=reuse_corpcode,
                     on_company=on_company, deep=bool(deep))


def _load_corp_codes(cfg: Config, run: SyncRun, key: str, redact, *, reuse: bool) -> tuple[list[dict], dict] | dict:
    """Corp codes from a fresh raw copy or a download: (rows, snapshot kwargs) or, when the run must stop, the
    closed run's summary."""
    summary = run.summary
    t0 = time.monotonic()
    fetched_at = store.now_utc()
    cached = find_cached_corpcode(cfg) if reuse else None
    if cached is not None:
        path, data, corp_codes = cached
        summary["corpcode_reused"] = 1
        run.log(f"corpCode: reusing {path} ({len(corp_codes)} corp codes)")
        return corp_codes, dict(request=run.request_record(CORPCODE_URL) | {"reused": True}, raw_path=str(path),
                                raw_sha256=store.sha256(data), raw_bytes=len(data), rows=len(corp_codes),
                                duration_s=round(time.monotonic() - t0, 3), note=f"reused_raw:{path}",
                                fetched_at=fetched_at)
    try:
        resp = run.fetch(with_key(CORPCODE_URL, KEY_PARAM, key), max_bytes=MAX_CORPCODE_BYTES,
                         deadline_s=CORPCODE_DEADLINE_S)
    except Blocked as e:
        run.mark_blocked(CORPCODE_URL, e.status, e.reason)
        summary.update(stopped_reason="blocked", blocked_at=CORPCODE_URL)
        return run.close(f"corpcode: {e.reason}")
    except KeyboardInterrupt:
        summary["stopped_reason"] = "interrupted"
        return run.close()
    except (OSError, http.client.HTTPException) as e:
        summary["stopped_reason"] = "corpcode_error"
        return run.close(err_note(redact, "corpcode: ", e))
    if resp.status != 200:
        summary["stopped_reason"] = "corpcode_error"
        return run.close(f"corpcode: http_{resp.status}")
    try:
        corp_codes = parse_corp_codes(resp.body)
    except ApiStop as e:
        if e.cooldown:
            run.mark_blocked(CORPCODE_URL, e.status, e.reason)
        summary.update(stopped_reason=e.reason, blocked_at=CORPCODE_URL,
                       stop_message=redact(e.message)[:300] if e.message else None)
        return run.close(f"corpcode: {e.reason}:{e.status}")
    except (DartStatusError, ValueError, zipfile.BadZipFile) as e:
        summary["stopped_reason"] = "corpcode_error"
        return run.close(err_note(redact, "corpcode: ", e))
    if not corp_codes:
        summary["stopped_reason"] = "corpcode_error"
        return run.close("corpcode: empty")
    problem = corpcode_problem(corp_codes)
    if problem:                  # a partial / wrong list: not saved, no snapshot, no identifier touched
        summary.update(stopped_reason="corpcode_error", corpcode_problem=problem)
        return run.close(f"corpcode: {problem}")
    body = redact(resp.body)
    raw_path, digest = store.save_raw(cfg, SOURCE_ID, "dart_corpcode", body, suffix="zip")
    return corp_codes, dict(request=run.request_record(CORPCODE_URL), raw_path=raw_path, raw_sha256=digest,
                            raw_bytes=len(body), rows=len(corp_codes), duration_s=round(time.monotonic() - t0, 3),
                            note=None, fetched_at=fetched_at)


class _DartRun(SyncRun):
    """SyncRun whose snapshot request records tell the website apart: in mode 'web' a batch whose URLs are all
    dart.fss.or.kr pages (the documents of a filing_batch) was fetched on rate key 'dart-web' without any key."""
    web = False

    def request_record(self, urls: Iterable[str] | str) -> dict:
        rec = super().request_record(urls)
        lst = [urls] if isinstance(urls, str) else list(urls)
        if self.web and lst and all(str(u).startswith(WEB_ORIGIN) for u in lst):
            rec.update(rate_key=WEB_RATE_KEY, auth="none",
                       fetched_via="dart.fss.or.kr dsaf001/main.do (TOC) + report/viewer.do (sections)")
        return rec


def _sync(cfg: Config, client: Any, key: str, redact, *, limit, codes, refresh, min_mcap_usd, lookback_days,
          batch_size, progress_every, only_universe, as_of, mode="web", reuse_corpcode=True,
          on_company=None, deep: bool = False) -> dict:
    run = _DartRun(cfg, source_id=SOURCE_ID, command=COMMAND, client=client, rate_key=RATE_KEY, redact=redact,
                  key_placeholder=KEY_PLACEHOLDER, log_tag="dart", statuses=CRAWL_STATUSES,
                  batch_size=batch_size, progress_every=progress_every)
    run.on_company = on_company
    run.web = mode == "web"
    summary = run.summary
    summary.update({"mode": mode, "corp_codes": 0, "ok_documents": 0, "web_sections": 0, "corpcode_reused": 0,
                    "deep": bool(deep)})
    today = as_of or dt.datetime.now(KST).date()
    bgn_de = (today - dt.timedelta(days=max(1, int(lookback_days)))).strftime("%Y%m%d")
    end_de = today.strftime("%Y%m%d")
    extractor = (DEEP_WEB_EXTRACTOR_VERSION if deep else WEB_EXTRACTOR_VERSION) if mode == "web" \
        else f"{EXTRACTOR_VERSION}/xml"
    section_name = DEEP_SECTION if deep else SECTION     # a deep text: its own row and file, never over the shallow
    pacer = WebPacer(WEB_MIN_INTERVAL_S)
    run.start()

    # ---- (2) corp codes ---------------------------------------------------------------------------------
    try:
        loaded = _load_corp_codes(cfg, run, key, redact, reuse=reuse_corpcode)
    except KeyboardInterrupt:
        summary["stopped_reason"] = "interrupted"
        return run.close()
    if isinstance(loaded, dict):
        return loaded
    corp_codes, snap_kw = loaded
    summary["corp_codes"] = len(corp_codes)
    report: dict = {}
    try:
        with store.session(cfg, wait_s=FINAL_WAIT_S) as con:
            snap = store.record_snapshot(con, source_id=SOURCE_ID, kind="dart_corpcode", **snap_kw)
            summary["snapshots"] += 1
            lines = filter_lines(load_lines(con, KR_VENUES, only_universe=only_universe), codes, min_mcap_usd)
            mapping = map_securities_to_corp(lines, corp_codes, report=report)
            run.write_mapping(con, lines, mapping, ID_TYPE, snap, report)
            stored = stored_documents(con, SOURCE_ID, section=section_name)
            run.attempts = dict(con.execute("SELECT security_id, attempts FROM crawl_state WHERE source_id = ?",
                                            [SOURCE_ID]).fetchall())
    except KeyboardInterrupt:
        summary["stopped_reason"] = "interrupted"
        return run.close()

    # ---- (3) queue --------------------------------------------------------------------------------------
    queue = run.queue_from_mapping(lines, mapping, limit)
    current: dict[str, str] = {}      # corp_code -> public URL of its latest request (for the Blocked marker)

    def api_get(corp: str, public_url: str, **kw):
        current[corp] = public_url
        return run.fetch(with_key(public_url, KEY_PARAM, key), **kw)

    def web_get(corp: str, url: str):
        """A dart.fss.or.kr page: own rate key and >= WEB_MIN_INTERVAL_S spacing; no key in the URL."""
        current[corp] = url
        pacer.wait()
        t = time.monotonic()
        try:
            resp = client.get(url, rate_key=WEB_RATE_KEY, deadline_s=WEB_DEADLINE_S, max_bytes=MAX_WEB_BYTES)
        finally:
            run.batch.duration_s += time.monotonic() - t
        summary["bytes_downloaded"] += len(resp.body or b"")
        return resp

    def doc_row(corp: str, filing: dict, raw: bytes, note: str, at: dt.datetime, partial: bool = False) -> dict:
        primary = run.groups[corp][0]
        rcept_no = filing["rcept_no"]
        return {"doc_id": f"{SOURCE_ID}:{corp}:{rcept_no}:{section_name}", "security_id": primary["security_id"],
                "company_key": primary["company_key"], "source_id": SOURCE_ID, "cik": None, "form": FORM_LABEL,
                "section": section_name, "accession": rcept_no, "filing_date": date_or_none(filing["rcept_dt"]),
                "report_date": filing["period_end"], "url": VIEWER_URL.format(rcept_no=rcept_no),
                "raw_sha256": store.sha256(raw), "raw_bytes": len(raw), "text_path": None, "text_sha256": None,
                "text_chars": None, "extractor": extractor + (PARTIAL_SUFFIX if partial else ""),
                "extract_note": redact(note), "fetched_at": at}

    def finish_item(corp: str, filing: dict, raw: bytes, section: str | None, desc_source: str | None, note: str,
                    at: dt.datetime, partial: bool = False) -> str:
        """partial=True: a part is missing for a temporary reason (products fetch failed, a body truncated); the
        row's extractor gets PARTIAL_SUFFIX so the skip test (rcept_no, extractor) fails and the next run redoes it."""
        for flag in filing["flags"]:
            note += f";{flag}"
        row = doc_row(corp, filing, raw, note, at, partial)
        if section is None:
            run.batch.docs.append(row)
            run.state(corp, "extract_failed", 200, note, at)
            return "extract_failed"
        tp = text_path_for(cfg, corp, filing["rcept_no"], section_name)
        tp.parent.mkdir(parents=True, exist_ok=True)
        data = section.encode("utf-8")
        tp.write_bytes(data)
        row.update(text_path=str(tp), text_sha256=store.sha256(data), text_chars=len(section))
        run.batch.docs.append(row)
        desc = None if deep else short_description_cjk(desc_source)    # a deep fetch never changes a profile
        if desc:
            run.add_description(corp, desc, "ko", row["url"], at, ID_TYPE)
        run.state(corp, "ok", 200, note, at)
        summary["ok_documents"] += 1
        return "ok"

    # ---- (4a) mode 'zip': document.xml ------------------------------------------------------------------
    def process_zip(corp: str, filing: dict, at: dt.datetime) -> str:
        rcept_no = filing["rcept_no"]
        # a stalled download raises http.RequestTimeout here; run_queue records 'error' / 'deadline;...' and
        # moves on to the next company
        dresp = api_get(corp, DOC_URL.format(rcept_no=rcept_no), max_bytes=MAX_DOC_BYTES, deadline_s=DOC_DEADLINE_S)
        if dresp.status != 200:
            run.state(corp, "error", dresp.status, f"document_http_{dresp.status}", at)
            return "error"
        raw = dresp.body or b""
        if not is_zip(raw):
            st, msg = envelope_status(raw)
            try:
                raise_for_status(st, msg)
                note = "document_not_zip"
            except DartStatusError as e:
                note = f"document_status_{e.status}"
            run.state(corp, "error", dresp.status, note, at)
            return "error"
        try:
            members = read_zip(raw)
            main = main_document(members, rcept_no)
            if main is None:
                section, xnote = None, "zip_without_xml"
            else:
                text, enc = decode_bytes(main[1], prefer=("utf-8", "cp949"))
                section, xnote = extract_overview(text)
                xnote += f";xml:{Path(main[0]).name};enc:{enc}"
        except (zipfile.BadZipFile, ValueError) as e:
            section, xnote = None, f"bad_zip:{str(e)[:60] or type(e).__name__}"
        truncated = bool(getattr(dresp, "truncated", False))
        if truncated:
            xnote += ";truncated"
        return finish_item(corp, filing, raw, section, section, xnote, at, partial=truncated)

    # ---- (4b) mode 'web': main.do TOC + viewer.do sections ----------------------------------------------
    refusals = [0]          # consecutive website refusals: main.do without TOC / 3xx, unverified or 3xx section
    shorts = [0]            # consecutive verified-but-too-short overview / chapter sections

    def refused(what: str) -> None:
        """One more website refusal; WEB_REFUSAL_STOP in a row stop the run like a block (cooldown marker)."""
        refusals[0] += 1
        if refusals[0] >= WEB_REFUSAL_STOP:
            raise ApiStop("access_refused", f"web:{what}", cooldown=True,
                          message=f"{refusals[0]} consecutive dart.fss.or.kr answers without report content")

    def too_short(corp: str, filing: dict, raw: bytes, n: int, parts: list[str], at: dt.datetime,
                  truncated: bool) -> str:
        if truncated:
            parts.append("truncated")
        status = finish_item(corp, filing, raw, None, None, f"section_too_short:{n};" + ";".join(parts), at,
                             partial=truncated)
        shorts[0] += 1
        if shorts[0] >= SHORT_SECTION_STOP:
            raise ApiStop("consecutive_errors", "section_too_short",
                          message=f"{shorts[0]} consecutive website sections under {MIN_SECTION_CHARS} chars")
        return status

    def location(resp: Any) -> str:
        loc = next((str(v) for k, v in (getattr(resp, "headers", None) or {}).items() if k.lower() == "location"), "")
        return f";location:{loc[:80]}" if loc else ""

    def page_note(body: bytes, html: str) -> str:
        """';bytes:<n>;sha:<12 hex>;title:<page title>' of a page that is not what was asked for (validate the
        website's real refusal / overload page on the first live run from these notes)."""
        m = re.search(r"<title\b[^>]*>(.*?)</title\s*>", html, re.S | re.I)
        title = _title_text(m.group(1))[:60] if m else ""
        return f";bytes:{len(body)};sha:{store.sha256(body)[:12]}" + (f";title:{title}" if title else "")

    def fetch_section(corp: str, node: Mapping[str, Any], rcept_no: str) -> tuple[Any, str, bool]:
        """(response, section text, verified); verified False for a 200 page that is not a report section."""
        resp = web_get(corp, section_url(node, rcept_no))
        if resp.status != 200:
            return resp, "", False
        html, _ = decode_bytes(resp.body or b"", prefer=("utf-8", "cp949"))
        if not section_verified(html, node.get("text")):
            return resp, "", False
        summary["web_sections"] += 1
        return resp, section_text(html, node.get("text")), True

    def section_failed(corp: str, resp: Any, at: dt.datetime) -> str:
        """Overview / chapter fetch without a verified section: crawl_state 'error' (retried next run, counted by
        the consecutive-error stop), no documents row; a 3xx or an unverified 200 page is also a refusal."""
        if resp.status == 200:
            note = "section_unverified" + page_note(resp.body or b"", decode_bytes(resp.body or b"",
                                                                                   prefer=("utf-8", "cp949"))[0])
            refused("section_unverified")
        else:
            note = f"section_http_{resp.status}" + location(resp)
            if 300 <= resp.status < 400:
                refused(f"section_http_{resp.status}")
        run.state(corp, "error", resp.status, note, at)
        return "error"

    def deep_blocks(corp: str, nodes: Sequence[Mapping[str, Any]], rcept_no: str, base: str,
                    raws: list[bytes]) -> tuple[tuple[str, bool, bool], str]:
        """((appendix, partial, truncated), note) of the deep sections (deep_sections.dart_deep_nodes); a failed
        fetch keeps what arrived and makes the row partial (redone by the next deep fetch)."""
        from .deep_sections import dart_appendix, dart_deep_nodes
        texts: list[tuple[str, str]] = []
        miss: list[str] = []
        part = trunc = False
        for name, node in dart_deep_nodes(nodes, title_key, lambda n: section_params(n) is not None):
            try:
                dresp, dtext, dok = fetch_section(corp, node, rcept_no)
            except RequestTimeout as e:
                miss.append(f"{name}_" + deadline_note(e).split(";", 1)[0])
                part = True
                continue
            except (OSError, http.client.HTTPException) as e:
                miss.append(f"{name}_network:{type(e).__name__}")
                part = True
                continue
            if dresp.status != 200 or not dok:
                miss.append(f"{name}_http_{dresp.status}" if dresp.status != 200 else f"{name}_unverified")
                part = True
                continue
            raws.append(dresp.body or b"")
            trunc |= bool(getattr(dresp, "truncated", False))
            if dtext:
                texts.append((name, dtext))
        app, note = dart_appendix(base, texts)
        return (app, part, trunc), ";".join([note] + miss)

    def process_web(corp: str, filing: dict, at: dt.datetime) -> str:
        rcept_no = filing["rcept_no"]
        mresp = web_get(corp, WEB_MAIN_URL.format(rcept_no=rcept_no))
        if mresp.status != 200:
            if 300 <= mresp.status < 400:
                refused(f"main_http_{mresp.status}")
            run.state(corp, "error", mresp.status, f"main_http_{mresp.status}" + location(mresp), at)
            return "error"
        main_html, _ = decode_bytes(mresp.body or b"", prefer=("utf-8", "cp949"))
        nodes = parse_toc(main_html)
        if not nodes:
            refused("no_toc")
            run.state(corp, "error", mresp.status, "no_toc" + page_note(mresp.body or b"", main_html), at)
            return "error"
        found = find_business_sections(nodes)
        parts = ["web"] + found["notes"]
        raws: list[bytes] = []
        truncated = bool(getattr(mresp, "truncated", False))
        partial = False
        if found["overview"] is not None:
            resp, overview, ok = fetch_section(corp, found["overview"], rcept_no)
            if not ok:
                return section_failed(corp, resp, at)
            refusals[0] = 0
            raws.append(resp.body or b"")
            truncated |= bool(getattr(resp, "truncated", False))
            parts.append("overview")
            if len(overview) < MIN_SECTION_CHARS:
                return too_short(corp, filing, raws[0], len(overview), parts, at, truncated)
            shorts[0] = 0
            if len(overview) > MAX_SECTION_CHARS:
                overview = overview[:MAX_SECTION_CHARS]
                parts.append("capped")
            products = ""
            if found["products"] is None:
                parts.append("no_products")
            else:
                partial = True            # until the products section really arrives
                try:
                    presp, products, pok = fetch_section(corp, found["products"], rcept_no)
                except RequestTimeout as e:       # keep the overview; the products item is optional
                    parts.append("products_" + deadline_note(e).split(";", 1)[0])
                except (OSError, http.client.HTTPException) as e:
                    parts.append(f"products_network:{type(e).__name__}")
                else:
                    if presp.status != 200:
                        parts.append(f"products_http_{presp.status}")
                    elif not pok:
                        parts.append("products_unverified")
                    else:
                        partial = False
                        raws.append(presp.body or b"")
                        truncated |= bool(getattr(presp, "truncated", False))
                        if products:
                            parts.append("products")
                            if len(products) > PRODUCTS_MAX_CHARS:
                                products = products[:PRODUCTS_MAX_CHARS]
                                parts.append("products_capped")
                        else:
                            parts.append("products_empty")
            section = overview + (f"\n\n{PRODUCTS_HEADING}\n{products}" if products else "")
            desc_source = overview
            if deep:
                deep_partial, deep_note = deep_blocks(corp, nodes, rcept_no, section, raws)
                section += deep_partial[0]
                partial = partial or deep_partial[1]
                truncated |= deep_partial[2]
                parts.append(deep_note)
        elif found["chapter"] is not None:
            resp, chapter, ok = fetch_section(corp, found["chapter"], rcept_no)
            if not ok:
                return section_failed(corp, resp, at)
            refusals[0] = 0
            raws.append(resp.body or b"")
            truncated |= bool(getattr(resp, "truncated", False))
            parts.append("fallback:chapter")
            if len(chapter) < MIN_SECTION_CHARS:
                return too_short(corp, filing, raws[0], len(chapter), parts, at, truncated)
            shorts[0] = 0
            if len(chapter) > CHAPTER_MAX_CHARS:
                chapter = chapter[:CHAPTER_MAX_CHARS]
                parts.append("capped")
            section = desc_source = chapter
            if deep:          # the chapter fallback is deepened too (else a deep row would hold a shallow text)
                deep_partial, deep_note = deep_blocks(corp, nodes, rcept_no, section, raws)
                section += deep_partial[0]
                partial = partial or deep_partial[1]
                truncated |= deep_partial[2]
                parts.append(deep_note)
        else:
            note = "business_section_not_found" if not any(_is_chapter(title_key(n.get("text"))) for n in nodes) \
                else "no_overview_in_toc"
            if truncated:
                parts.append("truncated")
            return finish_item(corp, filing, mresp.body or b"", None, None, f"{note};" + ";".join(parts), at,
                               partial=truncated)
        if truncated:
            parts.append("truncated")
        return finish_item(corp, filing, b"".join(raws), section, desc_source, ";".join(parts), at,
                           partial=partial or truncated)

    # ---- (4) per company --------------------------------------------------------------------------------
    def process(corp: str) -> str:
        at = store.now_utc()
        public_list = LIST_URL.format(corp_code=corp, bgn_de=bgn_de, end_de=end_de)
        resp = api_get(corp, public_list, deadline_s=LIST_DEADLINE_S)
        if resp.status != 200:
            run.state(corp, "error", resp.status, f"list_http_{resp.status}", at)
            return "error"
        try:
            payload = json.loads(resp.body)
        except ValueError:
            run.state(corp, "error", resp.status, "list_bad_json", at)
            return "error"
        if not isinstance(payload, Mapping):
            run.state(corp, "error", resp.status, "list_bad_json", at)
            return "error"
        status = str(payload.get("status") or "").strip()
        if status == STATUS_NO_DATA:
            run.state(corp, "no_annual_filing", 200, f"no_business_report_since_{bgn_de}", at)
            return "no_annual_filing"
        try:
            raise_for_status(status, payload.get("message"))
        except DartStatusError as e:
            run.state(corp, "error", resp.status, f"list_status_{e.status}", at)
            return "error"
        run.add_aux(f"list-{corp}", public_list, resp.body, at, corp_code=corp)
        filing = select_business_report(payload.get("list") or [], as_of=today)
        if filing is None:
            run.state(corp, "no_annual_filing", 200, f"no_business_report_since_{bgn_de}", at)
            return "no_annual_filing"
        if not refresh and stored.get(corp) == (filing["rcept_no"], extractor):
            summary["skipped_unchanged"] += 1
            return "skipped"
        if mode == "web":
            return process_web(corp, filing, at)
        return process_zip(corp, filing, at)

    def url_for(corp: str) -> str:
        return current.get(corp) or LIST_URL.format(corp_code=corp, bgn_de=bgn_de, end_de=end_de)

    run.run_queue(queue, process, url_for)
    return run.close()
