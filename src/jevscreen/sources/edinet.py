"""EDINET adapter: TradingView Japanese lines -> EDINET code -> latest 有価証券報告書 -> 事業の内容 (L2 evidence for Jev).

Source (provenance.py): 'edinet_yuho' (official-private). The section text is kept in local files under
<home>/docs/edinet/<edinet_code>/ and never shipped; only metadata goes into `documents`.

Flow (mirrors sources/sec_edgar.py):
1. cfg.edinet_api_key() is required (EdinetApiKeyMissing, raised before any request). The key travels as the
   `Subscription-Key` query parameter and is NEVER printed, logged or stored: every recorded URL is the key-free
   form, and every error text / note / marker is redacted (verbatim, URL-quoted and JSON-escaped forms) to
   '<edinet-api-key>' before it is truncated or written.
2. The key-free EDINET code list (Edinetcode.zip -> EdinetcodeDlInfo.csv, cp932) maps TradingView lines on JP venues
   (TSE, NAG, FSE, SAPSE) to EDINET codes: the 5-character 証券コード is the 4-character local code + '0'
   (also for the new alphanumeric codes: '285A' -> '285A0'). Only filers marked 上場 count; several listed filers
   on one code -> ambiguous, left unmapped. identifiers rows: id_type 'edinet_code', method 'sec_code'.
3. EDINET has no per-company filing search, so the daily lists (documents.json?date=D&type=2, one request per day)
   are scanned newest day first over the last `backfill_days` days (default 400). A day's list is cached under
   <home>/raw/edinet/lists/<date>.json once it is final (fetched FINAL_AFTER_DAYS or more days after that date), so
   later runs only fetch the newest days. The scan stops early once every queued company has a report.
   Each company's newest docTypeCode 120 / ordinanceCode 010 / formCode 030000 (有価証券報告書, domestic form 3)
   that is not withdrawn is its annual report.
4. documents/{docID}?type=5 returns a ZIP of CSVs converted from the XBRL; the jpcrp_cor:DescriptionOfBusinessTextBlock
   (事業の内容) value is extracted (UTF-16/UTF-8/cp932 and tab/comma handled; HTML stripped if present). 事業の内容 is
   short and structural (live check 2026-09-26: 8306, 9984, 7203), so jpcrp_cor:
   BusinessPolicyBusinessEnvironmentIssuesToAddressEtcTextBlock (経営方針、経営環境及び対処すべき課題等) is appended
   under the heading line '【経営方針、経営環境及び対処すべき課題等】', capped at POLICY_MAX_CHARS; extract_note
   'blocks:business[,policy]' says which blocks were found (extractor edinet-v2; v1 rows are re-extracted, which
   re-downloads the ~10 MB zip once). MIN_SECTION_CHARS and the short description use 事業の内容 alone.
5. Per batch, one short store.session() writes documents / descriptions / crawl_state. http.Blocked stops the whole
   run at once and writes the cooldown marker (guard.mark_blocked, command 'sync-edinet') before any DB write;
   a rejected key (JSON StatusCode 401, also inside an HTTP 200) or a JSON 429 stops it too; 5 consecutive errors
   stop it; Ctrl-C / SIGTERM flush and record 'interrupted'. Rate: rate_key 'edinet', >= 1.0 s between requests
   (the EDINET terms forbid heavy short-interval access). Missing values stay NULL.
   Deadlines (http.Client total wall-clock time per attempt): daily lists LIST_DEADLINE_S (60 s; a deadline is a
   per-day failure), document zips and the code list DOC_DEADLINE_S / CODELIST_DEADLINE_S (300 s). A document that
   hits its deadline is recorded 'error' with note 'deadline;...' ('stall;...' when a body read got nothing within
   the socket timeout) and the run goes on (the consecutive-error stop still applies).

SPEC-DERIVED, NOT YET SEEN LIVE (no API key existed when this was written; validate on the first live run and fix
the constants / parsers below if they differ):
- the unauthenticated / bad-key answer {"StatusCode": 401, "message": ...} with HTTP 200 (also handled: HTTP 401,
  and a metadata.status of "401");
- documents.json: {"metadata": {"status": "200", "message": "OK", ...}, "results": [{docID, edinetCode, secCode,
  docTypeCode, ordinanceCode, formCode, submitDateTime 'YYYY-MM-DD HH:MM', periodStart, periodEnd, withdrawalStatus,
  csvFlag, docDescription, ...}]} and metadata.status "404" for a date outside the retained range (only days older
  than RETENTION_DAYS are read that way; inside the range a 400/404 is a per-day failure, so a wrong endpoint stops
  the run through the consecutive-error rule instead of writing every company as 'no_annual_filing');
- the type=5 ZIP layout (XBRL_TO_CSV/jpcrp030000-asr-*.csv, UTF-16LE with BOM, tab-separated, header row with
  要素ID ... 値) and that the text-block value is plain text (HTML is stripped anyway if present);
- a type=5 error answer being a JSON envelope instead of a ZIP;
- EdinetcodeDlInfo.csv: first line is a download-date line, the header row follows (located by content, not index).

This module also hosts the small run engine shared with sources/dart.py (SyncRun, ApiKeyMissing, ApiStop, key
redaction, ZIP/encoding helpers, CJK short descriptions). It is written for both adapters; keep it generic.
"""
from __future__ import annotations

import contextlib
import csv
import datetime as dt
import http.client
import io
import json
import os
import re
import signal
import sys
import threading
import time
import unicodedata
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence
from urllib.parse import quote

from .. import guard, store
from ..config import Config
from ..config import redact as redact_secrets
from ..http import Blocked, Client, RequestTimeout

SOURCE_ID = "edinet_yuho"
API_BASE = "https://api.edinet-fsa.go.jp/api/v2"
LIST_URL = API_BASE + "/documents.json?date={date}&type=2"
DOC_URL = API_BASE + "/documents/{doc_id}?type=5"
CODELIST_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"
KEY_PARAM = "Subscription-Key"
KEY_PLACEHOLDER = "<edinet-api-key>"
KEY_FILE = "edinet_api_key"
KEY_ENV = "JEVSCREEN_EDINET_API_KEY"
RATE_KEY = "edinet"
DEFAULT_MIN_INTERVAL_S = 1.0
ID_TYPE = "edinet_code"
COMMAND = "sync-edinet"
EXTRACTOR_VERSION = "edinet-v2"     # v2: 経営方針… block appended (v1 rows are re-extracted on the next run)
FORM_LABEL = "有価証券報告書"
SECTION = "business"
ANNUAL_DOC_TYPE = "120"
ANNUAL_ORDINANCE = "010"
ANNUAL_FORM_CODE = "030000"
TEXT_BLOCK_ELEMENT = "jpcrp_cor:DescriptionOfBusinessTextBlock"
POLICY_BLOCK_ELEMENT = "jpcrp_cor:BusinessPolicyBusinessEnvironmentIssuesToAddressEtcTextBlock"
POLICY_HEADING = "【経営方針、経営環境及び対処すべき課題等】"
POLICY_MAX_CHARS = 8_000
LIST_DEADLINE_S = 60.0          # documents.json (one day)
DOC_DEADLINE_S = 300.0          # documents/{docID}?type=5 (~10 MB zip)
CODELIST_DEADLINE_S = 300.0     # Edinetcode.zip
DEFAULT_BACKFILL_DAYS = 400
MAX_BACKFILL_DAYS = 3650
RETENTION_DAYS = 10 * 365 - 14   # EDINET keeps ~10 years of lists; only older days may answer 400/404 as 'empty'
FINAL_AFTER_DAYS = 2          # a day's list is cached once fetched at least this many days after that date (JST)
JP_VENUES = frozenset({"TSE", "NAG", "FSE", "SAPSE"})
JST = dt.timezone(dt.timedelta(hours=9))

MIN_SECTION_CHARS = 80
MAX_SECTION_CHARS = 300_000
MAX_DOC_BYTES = 64 * 1024 * 1024
MAX_CODELIST_BYTES = 32 * 1024 * 1024
MAX_UNZIPPED_BYTES = 512 * 1024 * 1024
MAX_ZIP_MEMBERS = 4096
STALE_YEARS = 2

CRAWL_STATUSES = ("ok", "no_annual_filing", "no_csv", "extract_failed", "error", "blocked")

# ============================================================================================ shared run engine
# Used by this module and by sources/dart.py.

MAX_CONSECUTIVE_ERRORS = 5
FLUSH_WAIT_S = 120.0         # mid-run flush: wait this long for another process's lock, then keep the batch
FINAL_WAIT_S = 600.0         # final flush / run journal: wait longer, then raise StoreLocked
MAX_LOCKED_FLUSHES = 5       # consecutive locked mid-run flushes before the run stops ('store_locked')
MAX_PENDING = 500            # pending (unflushed) companies before the run stops ('store_locked')

# stopped_reason -> runs.status / summary['status'] (None -> 'ok')
STOP_RUN_STATUS = {
    "blocked": "blocked", "quota_exceeded": "blocked", "access_refused": "blocked",
    "consecutive_errors": "stopped_errors", "list_errors": "stopped_errors",
    "interrupted": "interrupted", "store_locked": "store_locked",
    "key_rejected": "error", "service_unavailable": "error", "codelist_error": "error", "corpcode_error": "error",
}


class ApiKeyMissing(KeyError):
    """The adapter's API key is not configured. Raised before any request. str() is the plain message."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else "API key missing"


class EdinetApiKeyMissing(ApiKeyMissing):
    """No EDINET Subscription-Key (env JEVSCREEN_EDINET_API_KEY or <home>/edinet_api_key)."""


class ApiStop(RuntimeError):
    """The API refused the run as a whole (key rejected, quota exceeded, access refused, maintenance).

    The run stops at once and keeps what it has. cooldown=True also writes the guard cooldown marker (like Blocked).
    Never carries the key: `message` is the provider's text, redacted by the caller before it is recorded."""

    def __init__(self, reason: str, status: Any = None, *, message: str | None = None, cooldown: bool = False):
        super().__init__(f"{reason} (status={status})")
        self.reason, self.status, self.message, self.cooldown = reason, status, message, cooldown


def key_redactor(secret: str, placeholder: str) -> Callable[[Any], Any]:
    """Replace the key (verbatim, URL-quoted, JSON-escaped) by `placeholder`. Redact BEFORE truncating."""
    variants = [secret, quote(secret, safe=""), quote(secret, safe="").lower()]

    def redact(s: Any) -> Any:
        if isinstance(s, (bytes, bytearray)):
            out = bytes(s)
            for v in variants:
                if v:
                    out = out.replace(v.encode("utf-8"), placeholder.encode("utf-8"))
            return out
        return redact_secrets(s, variants, placeholder)
    return redact


def with_key(public_url: str, param: str, key: str) -> str:
    """The request URL: the key-free public URL plus the key parameter (never recorded anywhere)."""
    return f"{public_url}{'&' if '?' in public_url else '?'}{param}={quote(key, safe='')}"


def err_note(redact: Callable[[Any], Any], prefix: str, e: BaseException, limit: int = 200) -> str:
    """'<prefix><Type>: <message>' redacted first, then cut to `limit` chars of message."""
    return redact(f"{prefix}{type(e).__name__}: {e}")[:len(prefix) + limit]


def deadline_note(e: RequestTimeout) -> str:
    """crawl_state note of a timed-out request: 'deadline;<seconds>s;received:<bytes>' (total deadline per attempt)
    or 'stall;<socket timeout>s;received:<bytes>' (a body read got nothing within the socket timeout)."""
    if getattr(e, "kind", "deadline") == "stall":
        return f"stall;{float(e.timeout_s or 0):g}s;received:{e.received}"
    return f"deadline;{e.deadline_s:g}s;received:{e.received}"


def prepare_client(cfg: Config, client: Any, min_interval_s: float) -> Any:
    """The given client (its min_interval_s raised to `min_interval_s` if lower) or a new polite http.Client."""
    if client is None:
        return Client(user_agent=cfg.user_agent, min_interval_s=min_interval_s, timeout_s=cfg.timeout_s)
    cur = getattr(client, "min_interval_s", None)
    if isinstance(cur, (int, float)) and cur < min_interval_s:
        client.min_interval_s = min_interval_s
    return client


@contextlib.contextmanager
def sigterm_as_interrupt() -> Iterator[None]:
    """Library callers: the first SIGTERM raises KeyboardInterrupt (flush + 'interrupted'), later ones are ignored.
    Installed only in the main thread and only when nobody else handles SIGTERM (the CLI installs its own)."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    try:
        current = signal.getsignal(signal.SIGTERM)
    except (ValueError, OSError):
        yield
        return
    if current not in (signal.SIG_DFL, None):
        yield
        return

    def handler(signum, frame):  # noqa: ARG001
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt("SIGTERM")

    try:
        signal.signal(signal.SIGTERM, handler)
    except (ValueError, OSError):
        yield
        return
    try:
        yield
    finally:
        with contextlib.suppress(ValueError, OSError, TypeError):
            signal.signal(signal.SIGTERM, current if current is not None else signal.SIG_DFL)


@contextlib.contextmanager
def sigterm_ignored() -> Iterator[None]:
    """Ignore SIGTERM while the final flush runs (a SIGTERM there would roll back the last batch); main thread only."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    try:
        previous = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    except (ValueError, OSError):
        yield
        return
    try:
        yield
    finally:
        with contextlib.suppress(ValueError, OSError, TypeError):
            signal.signal(signal.SIGTERM, previous if previous is not None else signal.SIG_DFL)


def is_zip(data: bytes | None) -> bool:
    return bool(data) and data[:4] == b"PK\x03\x04"


def read_zip(data: bytes, *, max_members: int = MAX_ZIP_MEMBERS,
             max_total: int = MAX_UNZIPPED_BYTES) -> list[tuple[str, bytes]]:
    """[(member name, bytes)] of a ZIP, directories skipped; refuses archives over the member / size caps
    (ValueError) before inflating them."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > max_members:
            raise ValueError(f"zip_too_many_members:{len(infos)}")
        if sum(i.file_size for i in infos) > max_total:
            raise ValueError("zip_too_large")
        return [(i.filename, zf.read(i)) for i in infos]


_DECL_RE = re.compile(rb"""(?:encoding|charset)\s*=\s*["']?([A-Za-z0-9_\-]+)""")


def decode_bytes(data: bytes, prefer: Sequence[str] = ("utf-8",)) -> tuple[str, str]:
    """(text, encoding). BOMs first (UTF-8, UTF-16 LE/BE), then BOM-less UTF-16 (NUL pattern), then an XML/HTML
    encoding declaration, then `prefer` in order, else the last preference with replacement characters."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace"), "utf-8-sig"
    if data.startswith(b"\xff\xfe"):
        return data[2:].decode("utf-16-le", "replace"), "utf-16-le"
    if data.startswith(b"\xfe\xff"):
        return data[2:].decode("utf-16-be", "replace"), "utf-16-be"
    head = data[:2000]
    if len(head) >= 4:
        odd_nuls = head[1::2].count(0)
        even_nuls = head[0::2].count(0)
        half = len(head) // 2
        if odd_nuls > half * 0.3 and even_nuls < half * 0.05:
            return data.decode("utf-16-le", "replace"), "utf-16-le"
        if even_nuls > half * 0.3 and odd_nuls < half * 0.05:
            return data.decode("utf-16-be", "replace"), "utf-16-be"
    tries: list[str] = []
    m = _DECL_RE.search(data[:512])
    if m:
        tries.append(m.group(1).decode("ascii").lower())
    tries += [p for p in prefer if p not in tries]
    for enc in tries:
        enc_n = {"ascii": "utf-8", "us-ascii": "utf-8", "euc-kr": "cp949", "ks_c_5601-1987": "cp949",
                 "shift_jis": "cp932", "shift-jis": "cp932", "sjis": "cp932", "x-sjis": "cp932"}.get(enc, enc)
        try:
            return data.decode(enc_n), enc_n
        except (LookupError, UnicodeDecodeError):
            continue
    last = (list(prefer) or ["utf-8"])[-1]
    return data.decode(last, "replace"), f"{last}+replace"


_WS_RE = re.compile(r"[ \t\r\f\v   -​  　]+")


def normalise_text(raw: str) -> str:
    """Collapse horizontal whitespace (incl. NBSP / ideographic space) per line; keep single and double breaks."""
    raw = raw.replace("\r\n", "\n").replace("\r", "\n").replace("­", "")
    out: list[str] = []
    blank = 0
    for ln in raw.split("\n"):
        ln = _WS_RE.sub(" ", ln).strip()
        if not ln:
            blank += 1
            continue
        if out:
            out.append("\n\n" if blank >= 1 else "\n")
        out.append(ln)
        blank = 0
    return "".join(out).strip()


_CJK_TERMINAL = "。．.!?！？」』）)"
_CJK_SENT_END_RE = re.compile(r"(?:[。！？]|[.!?](?=\s|$))[」』）)\"'”]?")
_NOTE_PARA_RE = re.compile(r"^(?:※|\(注|（注|注\s*[)）0-9０-９]|\[注|【注)")
_BRACKET_HEADING_RE = re.compile(r"^[\[［【〔<＜].{0,60}[\]］】〕>＞]$")
# lead-ins to a table / chart ('... は、次のとおりであります。', '... 다음과 같습니다.'): no content of their own
_LEAD_IN_RE = re.compile(r"(?:(?:次|以下)の(?:とおり|通り)(?:であります|です|となっております)|"
                         r"(?:다음|아래)과\s*같(?:습니다|다))[。．.]?$")


def _is_cjk_heading(p: str) -> bool:
    if _BRACKET_HEADING_RE.match(p):
        return True
    return len(p) <= 40 and not p.endswith(tuple(_CJK_TERMINAL))


def short_description_cjk(section_text: str | None, max_chars: int = 800, min_para: int = 20) -> str:
    """First meaningful paragraphs of a Japanese / Korean business section, at most max_chars characters.

    Skips headings (short lines without terminal punctuation, bracketed captions such as [事業系統図]), notes
    (※ / (注)), short lead-ins to a table or chart ('次のとおりであります。', '다음과 같습니다.') and fragments shorter
    than min_para; cuts at a sentence end (。 ！ ？ or . ! ? before a space) when
    possible. Returns '' when nothing qualifies."""
    if not section_text:
        return ""
    paras = [re.sub(r"\s+", " ", p).strip() for p in section_text.split("\n")]
    picked: list[str] = []
    total = 0
    for p in paras:
        if not p or len(p) < min_para or _is_cjk_heading(p) or _NOTE_PARA_RE.match(p) or \
                (len(p) <= 120 and _LEAD_IN_RE.search(p)):
            continue
        add = len(p) + (1 if picked else 0)
        if total + add <= max_chars:
            picked.append(p)
            total += add
            if total >= max_chars * 0.8:
                break
            continue
        room = max_chars - total - (1 if picked else 0)
        if room > 100 or not picked:
            cut = p[:room]
            ends = list(_CJK_SENT_END_RE.finditer(cut))
            if ends and ends[-1].end() > room * 0.4:
                cut = cut[:ends[-1].end()]
            else:
                cut = cut[:max(1, room - 1)].rstrip() + "…"
            picked.append(cut)
        break
    return "\n".join(picked)[:max_chars].strip()


def stale_flag(ref: dt.date | None, as_of: dt.date) -> str | None:
    if ref is not None and (as_of - ref).days > STALE_YEARS * 365.25:
        return f"stale:{int((as_of - ref).days // 365.25)}"
    return None


def date_or_none(s: Any) -> dt.date | None:
    """'2025-06-27', '2025-06-27 15:00', '20250627' -> date; anything else -> None."""
    if s is None:
        return None
    t = str(s).strip()
    m = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})", t)
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def load_lines(con, venues: Iterable[str], *, only_universe: bool = True) -> list[dict]:
    """Universe (or all active) lines on `venues` with company_key and market cap."""
    venues = sorted({v.upper() for v in venues})
    ph = ",".join("?" for _ in venues)
    if only_universe:
        sql = f"""SELECT security_id, exchange, symbol, company_key, market_cap_usd, tv_type FROM universe
                  WHERE upper(exchange) IN ({ph})"""
    else:
        sql = f"""SELECT s.security_id, s.exchange, s.symbol, s.company_key, lm.market_cap_usd, s.tv_type
                  FROM securities s LEFT JOIN latest_market lm USING (security_id)
                  WHERE s.active AND upper(s.exchange) IN ({ph})"""
    cols = ["security_id", "exchange", "symbol", "company_key", "market_cap_usd", "tv_type"]
    return [dict(zip(cols, r)) for r in con.execute(sql, venues).fetchall()]


def filter_lines(lines: Sequence[dict], codes: Iterable[str] | str | None,
                 min_mcap_usd: float | None) -> list[dict]:
    """Keep lines whose security_id or symbol is in `codes` (case-insensitive; None = all) and whose market cap is
    >= min_mcap_usd (None = no floor; a line without market cap fails any floor)."""
    wanted = None
    if codes is not None:
        if isinstance(codes, str):
            codes = [c for c in re.split(r"[,\s]+", codes) if c]
        wanted = {str(c).strip().upper() for c in codes if str(c).strip()}
    out = []
    for ln in lines:
        if wanted is not None and str(ln.get("security_id") or "").upper() not in wanted \
                and str(ln.get("symbol") or "").upper() not in wanted:
            continue
        if min_mcap_usd is not None:
            cap = ln.get("market_cap_usd")
            if cap is None or cap < float(min_mcap_usd):
                continue
        out.append(ln)
    return out


def split_sid(sec: Mapping[str, Any]) -> tuple[str, str]:
    sid = str(sec.get("security_id") or "")
    exch, _, sym = sid.partition(":")
    return str(sec.get("exchange") or exch).strip().upper(), str(sec.get("symbol") or sym).strip().upper()


def stored_documents(con, source_id: str) -> dict[str, tuple[str, str | None]]:
    """native id (2nd part of doc_id) -> (accession, extractor) of its newest stored document."""
    out: dict[str, tuple[str, str | None]] = {}
    for doc_id, acc, extractor in con.execute(
            "SELECT doc_id, accession, extractor FROM documents WHERE source_id = ? "
            "ORDER BY filing_date DESC NULLS LAST, fetched_at DESC", [source_id]).fetchall():
        parts = str(doc_id).split(":")
        if len(parts) >= 3:
            out.setdefault(parts[1], (acc, extractor))
    return out


_DOC_COLS = ["doc_id", "security_id", "company_key", "source_id", "cik", "form", "section", "accession",
             "filing_date", "report_date", "url", "raw_sha256", "raw_bytes", "text_path", "text_sha256", "text_chars",
             "extractor", "extract_note", "fetched_at", "snapshot_id"]
_DESC_COLS = ["security_id", "source_id", "company_key", "text", "text_sha256", "lang", "source_url", "fetched_at",
              "match_method", "match_score", "snapshot_id"]
_STATE_COLS = ["source_id", "security_id", "status", "http_status", "attempts", "last_attempt_at", "note"]
_ID_COLS = ["security_id", "id_type", "id_value", "method", "snapshot_id"]


class _Batch:
    def __init__(self) -> None:
        self.aux: list[dict] = []        # manifest entries of auxiliary payloads (lists, code lists)
        self.docs: list[dict] = []
        self.descs: list[dict] = []
        self.states: list[list[Any]] = []
        self.started_at: dt.datetime | None = None
        self.duration_s = 0.0
        self.items = 0

    def __len__(self) -> int:
        return len(self.states) + len(self.aux) + len(self.docs)


class SyncRun:
    """Journal, batching, flushing and the per-company loop shared by the EDINET and DART adapters.

    No DB connection is held during network I/O: every DB touch is a short store.session(). The key never reaches
    anything this class records: URLs given to it are key-free (the request URL is built by the caller) and every
    note / error / marker text passes through `redact` first."""

    def __init__(self, cfg: Config, *, source_id: str, command: str, client: Any, rate_key: str,
                 redact: Callable[[Any], Any], key_placeholder: str, log_tag: str, statuses: Sequence[str],
                 batch_size: int = 25, progress_every: int = 100) -> None:
        self.cfg, self.source_id, self.command, self.client = cfg, source_id, command, client
        self.rate_key, self.redact, self.key_placeholder, self.log_tag = rate_key, redact, key_placeholder, log_tag
        self.batch_size = max(1, int(batch_size))
        self.progress_every = progress_every
        self.run_id = f"{command}-{uuid.uuid4().hex[:12]}"
        self.requests_before = int(getattr(client, "requests_made", 0) or 0)
        self.counts = {s: 0 for s in statuses}
        self.groups: dict[str, list[dict]] = {}
        self.attempts: dict[str, int] = {}
        self.batch = _Batch()
        self.locked_flushes = 0
        self.next_flush_at = self.batch_size
        # on_company(security_ids, status, note): per-company events for the on-demand fetch (main thread; errors
        # ignored): every recorded status, and 'skipped_unchanged' for a company process() settled without a status
        self.on_company: Callable[[list[str], str, str | None], None] | None = None
        self.summary: dict[str, Any] = {
            "run_id": self.run_id, "lines": 0, "securities_mapped": 0, "ambiguous": [], "mapped_by_method": {},
            "identifiers_removed": 0, "companies_queued": 0, "companies_attempted": 0, "skipped_unchanged": 0,
            "ok": 0, "failures_by_note": {}, "requests": 0, "bytes_downloaded": 0, "snapshots": 0,
            "stopped_reason": None, "blocked_at": None, "status": None}

    # ---- journal ---------------------------------------------------------------------------------------------
    def log(self, msg: str) -> None:
        print(f"[{self.log_tag}] {self.redact(msg)}", file=sys.stderr, flush=True)

    def start(self) -> None:
        with store.session(self.cfg, wait_s=FINAL_WAIT_S) as con:
            con.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
                        [self.run_id, self.command, store.now_utc(), None, "running", 0, None])

    def finish(self, status: str, note: Any = None) -> None:
        self.summary["requests"] = int(getattr(self.client, "requests_made", 0) or 0) - self.requests_before
        self.summary["status"] = status
        payload = self.redact(json.dumps(
            {"counts": self.counts, "stopped_reason": self.summary["stopped_reason"],
             **({"note": self.redact(note)} if note else {})}, default=str, ensure_ascii=False))
        with store.session(self.cfg, wait_s=FINAL_WAIT_S) as con:
            con.execute("UPDATE runs SET finished_at = ?, status = ?, requests = ?, note = ? WHERE run_id = ?",
                        [store.now_utc(), status, self.summary["requests"], payload, self.run_id])

    def close(self, note: Any = None) -> dict:
        """Final flush (SIGTERM ignored meanwhile) and the runs row; returns the summary."""
        stopped = self.summary["stopped_reason"]
        run_status = STOP_RUN_STATUS.get(stopped, "ok") if stopped else "ok"
        self.summary.update({f"status_{k}": v for k, v in self.counts.items()})
        with sigterm_ignored():
            try:
                self.flush(note=f"run stopped: {stopped}" if stopped else None)
            finally:
                self.finish(run_status, note)     # attempted even when the final flush fails
        return self.summary

    # ---- network ---------------------------------------------------------------------------------------------
    def fetch(self, request_url: str, **kw):
        t = time.monotonic()
        try:
            resp = self.client.get(request_url, rate_key=self.rate_key, **kw)
        finally:
            self.batch.duration_s += time.monotonic() - t
        self.summary["bytes_downloaded"] += len(resp.body or b"")
        return resp

    def mark_blocked(self, public_url: str | None, status: Any, reason: Any) -> None:
        """Cooldown marker on disk first: it survives a final DB write that fails (StoreLocked, crash)."""
        guard.mark_blocked(self.cfg, self.command, url=self.redact(public_url), status=status,
                           reason=self.redact(str(reason) if reason is not None else None))

    def request_record(self, urls: Iterable[str] | str) -> dict:
        rec: dict[str, Any] = {"method": "GET", "rate_key": self.rate_key, "auth": f"query:{self.key_placeholder}"}
        if isinstance(urls, str):
            rec["url"] = self.redact(urls)
        else:
            rec["urls"] = [self.redact(u) for u in urls]
        return rec

    # ---- batch -----------------------------------------------------------------------------------------------
    def event(self, key: str, status: str, note: str | None) -> None:
        if self.on_company is not None:
            try:
                self.on_company([ln["security_id"] for ln in self.groups.get(key, [])], status, self.redact(note))
            except Exception:  # noqa: BLE001 - a callback never stops the sync
                pass

    def state(self, key: str, status: str, http_status: int | None, note: str | None, at: dt.datetime) -> None:
        for ln in self.groups[key]:
            sid = ln["security_id"]
            self.batch.states.append([self.source_id, sid, status, http_status, (self.attempts.get(sid) or 0) + 1,
                                      at, note])
        self.event(key, status, note)
        self.counts[status] = self.counts.get(status, 0) + 1
        if status == "ok":
            self.summary["ok"] += 1
        else:
            k = (note or status).split(":", 1)[0].split(";", 1)[0].strip() or status
            self.summary["failures_by_note"][k] = self.summary["failures_by_note"].get(k, 0) + 1

    def add_aux(self, kind: str, public_url: str, body: bytes, at: dt.datetime, *, save: bool = True,
                **extra: Any) -> None:
        """Record an auxiliary payload (per-company list JSON, ...) for the next flush; raw saved (redacted)."""
        body = self.redact(body)
        path = digest = None
        if save:
            path, digest = store.save_raw(self.cfg, self.source_id, kind, body)
        self.batch.aux.append({"kind": kind, "url": self.redact(public_url), "raw_path": path,
                               "raw_sha256": digest or store.sha256(body), "raw_bytes": len(body),
                               "fetched_at": at.isoformat(), **extra})

    def add_description(self, key: str, text: str, lang: str, source_url: str, at: dt.datetime,
                        match_method: str) -> None:
        for ln in self.groups[key]:
            self.batch.descs.append({"security_id": ln["security_id"], "source_id": self.source_id,
                                     "company_key": ln["company_key"], "text": text,
                                     "text_sha256": store.sha256(text.encode("utf-8")), "lang": lang,
                                     "source_url": source_url, "fetched_at": at, "match_method": match_method,
                                     "match_score": 1.0})

    def flush(self, note: str | None = None, *, wait_s: float = FINAL_WAIT_S) -> None:
        """One short write session: snapshots + documents + descriptions + crawl_state, atomically.

        Raises store.StoreLocked when the lock outlasts `wait_s`; the batch is kept then (the caller decides)."""
        batch = self.batch
        if not len(batch):
            self.batch = _Batch()
            return
        redact = self.redact

        def iso(v: Any) -> Any:
            return v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v

        aux_manifest = (redact(json.dumps(batch.aux, sort_keys=True, ensure_ascii=False)).encode("utf-8")
                        if batch.aux else None)
        doc_manifest = (redact(json.dumps([{k: iso(v) for k, v in d.items()} for d in batch.docs], sort_keys=True,
                                          ensure_ascii=False)).encode("utf-8") if batch.docs else None)
        snaps = 0
        with store.session(self.cfg, wait_s=wait_s) as con:
            aux_path = aux_digest = doc_path = doc_digest = None
            if aux_manifest is not None:
                aux_path, aux_digest = store.save_raw(self.cfg, self.source_id, "aux_batch", aux_manifest)
            if doc_manifest is not None:
                doc_path, doc_digest = store.save_raw(self.cfg, self.source_id, "filing_batch", doc_manifest)
            con.begin()
            try:
                aux_id = None
                if batch.aux:
                    aux_id = store.record_snapshot(
                        con, source_id=self.source_id, kind="aux_batch",
                        request=self.request_record([a["url"] for a in batch.aux]), raw_path=aux_path,
                        raw_sha256=aux_digest, raw_bytes=len(aux_manifest), rows=len(batch.aux),
                        duration_s=round(batch.duration_s, 3), note=redact(note), fetched_at=batch.started_at)
                    snaps += 1
                snap_id = None
                if batch.docs:
                    snap_id = store.record_snapshot(
                        con, source_id=self.source_id, kind="filing_batch",
                        request=self.request_record([d["url"] for d in batch.docs]), raw_path=doc_path,
                        raw_sha256=doc_digest, raw_bytes=len(doc_manifest), rows=len(batch.docs),
                        duration_s=round(batch.duration_s, 3), note=redact(note), fetched_at=batch.started_at)
                    snaps += 1
                store.upsert_many(con, "documents", _DOC_COLS,
                                  [[d.get(c) if c != "snapshot_id" else snap_id for c in _DOC_COLS]
                                   for d in batch.docs])
                # descriptions point to the filing batch; a batch without documents (e.g. MOPS basic data, whose
                # payloads are aux entries) points them to its aux batch instead of leaving snapshot_id NULL
                store.upsert_many(con, "descriptions", _DESC_COLS,
                                  [[d.get(c) if c != "snapshot_id" else (snap_id or aux_id) for c in _DESC_COLS]
                                   for d in batch.descs])
                store.upsert_many(con, "crawl_state", _STATE_COLS,
                                  [[r[0], r[1], r[2], r[3], r[4], r[5], redact(r[6])] for r in batch.states])
                con.commit()
            except BaseException:
                con.rollback()
                raise
        for r in batch.states:
            self.attempts[r[1]] = r[4]
        self.summary["snapshots"] += snaps
        self.batch = _Batch()

    def flush_mid_run(self) -> None:
        """Short wait; on another process's lock keep the batch and retry after another batch_size companies."""
        try:
            self.flush(wait_s=FLUSH_WAIT_S)
        except store.StoreLocked:
            self.locked_flushes += 1
            self.next_flush_at = self.batch.items + self.batch_size
            self.log(f"store locked by another process; keeping {self.batch.items} pending companies "
                     f"(locked flush {self.locked_flushes}/{MAX_LOCKED_FLUSHES})")
            if self.locked_flushes >= MAX_LOCKED_FLUSHES or self.batch.items >= MAX_PENDING:
                self.summary["stopped_reason"] = "store_locked"
            return
        self.locked_flushes = 0
        self.next_flush_at = self.batch_size

    # ---- loop ------------------------------------------------------------------------------------------------
    def run_queue(self, queue: Sequence[str], process: Callable[[str], str],
                  url_for: Callable[[str], str]) -> None:
        """process(key) does the network + parsing of one company and returns its status ('skipped' allowed).

        http.Blocked / ApiStop stop the whole run (marker first); http.RequestTimeout counts as 'error' with note
        'deadline;...'; other OSError / HTTPException count as 'error' ('network: ...');
        MAX_CONSECUTIVE_ERRORS consecutive errors stop the run; KeyboardInterrupt -> 'interrupted'; anything
        else flushes, journals 'error' and re-raises."""
        consecutive = 0
        try:
            for key in queue:
                self.summary["companies_attempted"] += 1
                if self.batch.started_at is None:
                    self.batch.started_at = store.now_utc()
                try:
                    status = process(key)
                except Blocked as e:
                    self.mark_blocked(url_for(key), e.status, e.reason)
                    self.state(key, "blocked", e.status, f"blocked: {e.reason}", store.now_utc())
                    self.summary.update(stopped_reason="blocked", blocked_at=key)
                    break
                except ApiStop as e:
                    if e.cooldown:
                        self.mark_blocked(url_for(key), e.status, e.reason)
                        self.state(key, "blocked", None, self.redact(f"{e.reason}:{e.status}"), store.now_utc())
                    self.summary.update(stopped_reason=e.reason, blocked_at=key,
                                        stop_message=self.redact(e.message)[:300] if e.message else None)
                    break
                except RequestTimeout as e:     # total deadline of one request: an item error, the run goes on
                    self.state(key, "error", e.status, deadline_note(e), store.now_utc())
                    status = "error"
                except (OSError, http.client.HTTPException) as e:  # URLError is an OSError
                    self.state(key, "error", None, err_note(self.redact, "network: ", e), store.now_utc())
                    status = "error"
                if status == "skipped":
                    self.event(key, "skipped_unchanged", None)
                consecutive = consecutive + 1 if status == "error" else 0
                if consecutive >= MAX_CONSECUTIVE_ERRORS:
                    self.summary["stopped_reason"] = "consecutive_errors"
                    break
                self.batch.items += 1
                if self.batch.items >= self.next_flush_at:
                    self.flush_mid_run()
                    if self.summary["stopped_reason"]:
                        break
                if self.progress_every and self.summary["companies_attempted"] % self.progress_every == 0:
                    self.log(f"{self.summary['companies_attempted']}/{len(queue)} {self.counts}")
        except KeyboardInterrupt:
            self.summary["stopped_reason"] = "interrupted"
        except BaseException:
            self.summary["stopped_reason"] = "crashed"
            with sigterm_ignored():
                try:
                    self.flush(note="run stopped: crashed")
                finally:
                    self.finish("error", "crashed")
            raise

    def queue_from_mapping(self, lines: Sequence[dict], mapping: Sequence[tuple[str, str, str]],
                           limit: int | None) -> list[str]:
        """Group mapped lines by native id; queue native ids by their largest line's market cap (desc)."""
        by_sid = {ln["security_id"]: ln for ln in lines}
        groups: dict[str, list[dict]] = {}
        for sid, native, _ in mapping:
            groups.setdefault(native, []).append(by_sid[sid])
        for g in groups.values():
            g.sort(key=lambda ln: (-(ln["market_cap_usd"] if ln["market_cap_usd"] is not None else float("-inf")),
                                   ln["security_id"]))
        self.groups = groups

        def cap(k: str) -> float:
            c = groups[k][0]["market_cap_usd"]
            return c if c is not None else float("-inf")

        queue = sorted(groups, key=lambda k: (-cap(k), k))
        if limit is not None:
            queue = queue[:max(0, int(limit))]
        self.summary["companies_queued"] = len(queue)
        return queue

    def write_mapping(self, con, lines: Sequence[dict], mapping: Sequence[tuple[str, str, str]], id_type: str,
                      snapshot_id: str, report: Mapping[str, Any]) -> None:
        """identifiers upsert, and removal of `id_type` rows of considered lines that no longer map."""
        store.upsert_many(con, "identifiers", _ID_COLS,
                          [[sid, id_type, str(native), method, snapshot_id] for sid, native, method in mapping])
        mapped = {sid for sid, _, _ in mapping}
        unmapped = sorted({ln["security_id"] for ln in lines} - mapped)
        if unmapped:
            self.summary["identifiers_removed"] = con.execute(
                "SELECT count(*) FROM identifiers WHERE id_type = ? AND list_contains(?::VARCHAR[], security_id)",
                [id_type, unmapped]).fetchone()[0]
            con.execute("DELETE FROM identifiers WHERE id_type = ? AND list_contains(?::VARCHAR[], security_id)",
                        [id_type, unmapped])
        self.summary["lines"] = len(lines)
        self.summary["securities_mapped"] = len(mapping)
        self.summary["ambiguous"] = list(report.get("ambiguous", []))
        for _, _, m in mapping:
            self.summary["mapped_by_method"][m] = self.summary["mapped_by_method"].get(m, 0) + 1


# ============================================================================================ EDINET code list


def _nfkc(s: Any) -> str:
    return unicodedata.normalize("NFKC", str(s or "")).strip()


def _codelist_columns(header: Sequence[str]) -> dict[str, int] | None:
    h = [_nfkc(c).strip('"') for c in header]

    def find(pred: Callable[[str], bool]) -> int:
        return next((i for i, c in enumerate(h) if pred(c)), -1)

    cols = {
        "edinet_code": find(lambda c: "EDINET" in c.upper() and "コード" in c),
        "filer_type": find(lambda c: "提出者種別" in c),
        "listed": find(lambda c: "上場区分" in c),
        "name": find(lambda c: "提出者名" in c and "英字" not in c and "ヨミ" not in c),
        "name_en": find(lambda c: "提出者名" in c and "英字" in c),
        "sec_code": find(lambda c: "証券コード" in c),
        "jcn": find(lambda c: "法人番号" in c),
    }
    if cols["edinet_code"] < 0 or cols["sec_code"] < 0:
        return None
    return cols


def parse_codelist(data: bytes | str) -> list[dict]:
    """Edinetcode.zip (or the bare EdinetcodeDlInfo.csv, bytes in cp932 / UTF-8, or text) ->
    [{edinet_code, sec_code (5 chars, upper), listed (True/False/None), name, name_en, filer_type, jcn}].

    The header row is located by content within the first 5 rows (the file starts with a download-date line).
    Rows without an EDINET code are skipped; sec_code is None when blank."""
    if isinstance(data, (bytes, bytearray)):
        data = bytes(data)
        if is_zip(data):
            members = [(n, b) for n, b in read_zip(data) if n.lower().endswith(".csv")]
            if not members:
                raise ValueError("codelist_zip_without_csv")
            members.sort(key=lambda nb: (0 if "edinetcode" in nb[0].lower() else 1, nb[0]))
            data = members[0][1]
        text, _ = decode_bytes(data, prefer=("utf-8", "cp932"))
    else:
        text = data
    rows = list(csv.reader(io.StringIO(text)))
    cols = None
    start = 0
    for i, row in enumerate(rows[:5]):
        cols = _codelist_columns(row)
        if cols:
            start = i + 1
            break
    if not cols:
        raise ValueError("codelist_header_not_found")

    def cell(row: Sequence[str], k: str) -> str | None:
        i = cols[k]
        if i < 0 or i >= len(row):
            return None
        v = _nfkc(row[i])
        return v or None

    out = []
    for row in rows[start:]:
        code = cell(row, "edinet_code")
        if not code:
            continue
        listed_raw = cell(row, "listed")
        sec_code = cell(row, "sec_code")
        out.append({"edinet_code": code.upper(), "sec_code": sec_code.upper() if sec_code else None,
                    "listed": None if listed_raw is None else listed_raw == "上場",
                    "name": cell(row, "name"), "name_en": cell(row, "name_en"),
                    "filer_type": cell(row, "filer_type"), "jcn": cell(row, "jcn")})
    return out


def sec_code_for_symbol(symbol: str) -> str | None:
    """TradingView local code -> EDINET 5-character 証券コード: '7203' -> '72030', '285A' -> '285A0';
    a 5-character code (preferred shares etc.) is used as is. Anything else -> None."""
    s = _nfkc(symbol).upper()
    if re.fullmatch(r"[0-9A-Z]{4}", s):
        return s + "0"
    if re.fullmatch(r"[0-9A-Z]{5}", s):
        return s
    return None


def map_securities_to_edinet(securities: Sequence[Mapping[str, Any]], codelist: Sequence[Mapping[str, Any]], *,
                             report: dict | None = None) -> list[tuple[str, str, str]]:
    """Map TradingView lines on JP venues to EDINET codes. Returns [(security_id, edinet_code, 'sec_code')].

    Only filers marked 上場 are candidates (a code list without the 上場区分 column accepts all). Exactly one EDINET
    code per line; several listed candidates -> report['ambiguous']. `report` receives: considered, non_jp,
    unmatched, ambiguous."""
    index: dict[str, set[str]] = {}
    for e in codelist:
        sc = e.get("sec_code")
        if not sc or e.get("listed") is False:
            continue
        index.setdefault(str(sc).upper(), set()).add(str(e["edinet_code"]).upper())
    rep = {"considered": 0, "non_jp": 0, "unmatched": 0, "ambiguous": []}
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for sec in securities:
        sid = str(sec.get("security_id") or "")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        exch, sym = split_sid(sec)
        if exch not in JP_VENUES or not sym:
            rep["non_jp"] += 1
            continue
        rep["considered"] += 1
        key = sec_code_for_symbol(sym)
        cands = index.get(key or "")
        if not cands:
            rep["unmatched"] += 1
            continue
        if len(cands) > 1:
            rep["ambiguous"].append({"security_id": sid, "sec_code": key, "edinet_codes": sorted(cands)})
            continue
        out.append((sid, next(iter(cands)), "sec_code"))
    if report is not None:
        report.update(rep)
    return out


# ============================================================================================ daily lists


class EdinetStatusError(RuntimeError):
    """A non-OK EDINET JSON status that concerns only this request (not the whole run)."""

    def __init__(self, status: str, message: str | None = None):
        super().__init__(f"edinet status {status}: {message or ''}".strip())
        self.status, self.message = status, message


def api_status(payload: Any) -> tuple[str | None, str | None]:
    """(status, message) of an EDINET JSON answer: top-level StatusCode (auth failures) or metadata.status."""
    if not isinstance(payload, Mapping):
        return None, None
    for k in ("StatusCode", "statusCode"):
        if k in payload:
            return str(payload.get(k)).strip(), payload.get("message")
    md = payload.get("metadata")
    if isinstance(md, Mapping) and md.get("status") is not None:
        return str(md.get("status")).strip(), md.get("message")
    return None, None


def check_status(payload: Any) -> None:
    """Raise ApiStop for statuses that concern the whole run (401 key rejected, 403 refused, 429 too many) and
    EdinetStatusError for any other non-200 status. A missing status counts as OK."""
    status, msg = api_status(payload)
    if status in (None, "", "200"):
        return
    if status == "401":
        raise ApiStop("key_rejected", 401, message=str(msg) if msg else None)
    if status == "403":
        raise ApiStop("access_refused", 403, message=str(msg) if msg else None, cooldown=True)
    if status == "429":
        raise ApiStop("quota_exceeded", 429, message=str(msg) if msg else None, cooldown=True)
    raise EdinetStatusError(status, str(msg) if msg else None)


def parse_documents_list(payload: Any) -> list[dict]:
    """documents.json (type=2) -> its result rows. Raises ApiStop / EdinetStatusError (see check_status), also for
    metadata.status 400/404: the caller decides whether the day is outside the retained range (outside_retention)
    or the request is wrong (a per-day failure)."""
    if isinstance(payload, (bytes, str)):
        payload = json.loads(payload)
    check_status(payload)
    rows = payload.get("results") if isinstance(payload, Mapping) else None
    return [r for r in (rows or []) if isinstance(r, Mapping)]


def outside_retention(day: dt.date, today: dt.date) -> bool:
    """True for a day older than EDINET's retained range (about ten years), where 400/404 means 'no list'."""
    return (today - day).days > RETENTION_DAYS


def is_unusable_copy(row: Mapping[str, Any]) -> bool:
    """Withdrawn (withdrawalStatus != '0') or not disclosed (disclosureStatus '2', 不開示)."""
    return (str(row.get("withdrawalStatus") or "0") != "0"
            or str(row.get("disclosureStatus") or "0") == "2")


def is_annual_report(row: Mapping[str, Any]) -> bool:
    """有価証券報告書 of a domestic company (docTypeCode 120, ordinanceCode 010, formCode 030000), not withdrawn and
    not 不開示."""
    return (str(row.get("docTypeCode") or "") == ANNUAL_DOC_TYPE
            and str(row.get("ordinanceCode") or "") == ANNUAL_ORDINANCE
            and str(row.get("formCode") or "") == ANNUAL_FORM_CODE
            and not is_unusable_copy(row)
            and bool(row.get("docID")) and bool(row.get("edinetCode")))


def _filing_from_row(row: Mapping[str, Any], day: dt.date) -> dict:
    return {"doc_id": str(row["docID"]), "edinet_code": str(row["edinetCode"]).upper(),
            "sec_code": row.get("secCode"), "filer_name": row.get("filerName"),
            "submit_datetime": str(row.get("submitDateTime") or day.isoformat()),
            "period_start": row.get("periodStart"), "period_end": row.get("periodEnd"),
            "doc_description": row.get("docDescription"), "csv_flag": str(row.get("csvFlag") or ""),
            "list_date": day.isoformat()}


def pick_latest(filings: Iterable[Mapping[str, Any]]) -> dict[str, dict]:
    """edinet_code -> newest filing (submitDateTime, then periodEnd, then docID; all descending)."""
    best: dict[str, dict] = {}
    for f in filings:
        k = f["edinet_code"]
        cur = best.get(k)
        key = (str(f.get("submit_datetime") or ""), str(f.get("period_end") or ""), str(f.get("doc_id") or ""))
        if cur is None or key > (str(cur.get("submit_datetime") or ""), str(cur.get("period_end") or ""),
                                 str(cur.get("doc_id") or "")):
            best[k] = dict(f)
    return best


def list_cache_path(cfg: Config, day: dt.date) -> Path:
    return Path(cfg.home) / "raw" / "edinet" / "lists" / f"{day.isoformat()}.json"


# ============================================================================================ type=5 CSV


_TAG_RE = re.compile(r"<[A-Za-z/!][^>]*>")


def parse_xbrl_csv(text: str) -> list[list[str]]:
    """Rows of an EDINET XBRL-to-CSV file (tab-separated in EDINET's conversion; comma accepted)."""
    first = text.split("\n", 1)[0]
    delim = "\t" if "\t" in first else ","
    old = csv.field_size_limit()
    csv.field_size_limit(max(old, 64 * 1024 * 1024))
    try:
        return list(csv.reader(io.StringIO(text), delimiter=delim))
    finally:
        csv.field_size_limit(old)


def find_element_value(rows: Sequence[Sequence[str]], element: str = TEXT_BLOCK_ELEMENT) -> str | None:
    """The value of `element` (longest if several contexts carry it). Columns come from the header (要素ID, 値);
    without a recognisable header the first column is the element id and the last the value."""
    if not rows:
        return None
    header = [_nfkc(c).strip('"').lstrip("﻿") for c in rows[0]]
    id_col = next((i for i, c in enumerate(header) if c in ("要素ID", "要素id", "ElementID", "Element ID")), None)
    val_col = next((i for i, c in enumerate(header) if c in ("値", "Value")), None)
    body = rows[1:] if id_col is not None or val_col is not None else rows
    id_col = 0 if id_col is None else id_col
    hits = []
    for row in body:
        if len(row) <= id_col:
            continue
        if row[id_col].strip().strip('"').lstrip("﻿") != element:
            continue
        vc = val_col if val_col is not None and val_col < len(row) else len(row) - 1
        hits.append(row[vc])
    return max(hits, key=len) if hits else None


def text_block_to_text(value: str) -> str:
    """Text-block value -> plain text (HTML stripped when present), whitespace normalised."""
    if _TAG_RE.search(value):
        from .sec_edgar import html_to_text   # stdlib/lxml HTML -> text with paragraph breaks
        return normalise_text(html_to_text(value))
    import html as htmlmod
    return normalise_text(htmlmod.unescape(value))


_POLICY_HEADING_RE = re.compile(r"^[【\[［]?\s*経営方針[、,，]?\s*経営環境及び対処すべき課題等\s*[】\]］]?[ \t]*(?:\n+|$)")


def policy_text(value: str) -> tuple[str, bool]:
    """経営方針… text-block value -> (plain text without its own heading line, capped?). Capped at POLICY_MAX_CHARS,
    cut back to the last sentence end in the final fifth when there is one."""
    text = _POLICY_HEADING_RE.sub("", text_block_to_text(value), count=1).strip()
    if len(text) <= POLICY_MAX_CHARS:
        return text, False
    cut = text[:POLICY_MAX_CHARS]
    end = max(cut.rfind("。"), cut.rfind("\n"))
    if end >= POLICY_MAX_CHARS * 0.8:
        cut = cut[:end + 1]
    return cut.rstrip(), True


def extract_business_section(zip_bytes: bytes) -> tuple[str | None, str]:
    """type=5 ZIP -> (text or None, note). Searches the jpcrp CSV first, then the other CSV members.

    The text is 事業の内容 (DescriptionOfBusinessTextBlock, required) followed, when present, by a blank line, the
    heading line POLICY_HEADING and 経営方針、経営環境及び対処すべき課題等 (POLICY_BLOCK_ELEMENT, capped at
    POLICY_MAX_CHARS; searched in the same CSV first, then in the others). The note names the CSV, its encoding and
    'blocks:business[,policy]' (plus 'policy_capped' / 'policy_csv:<name>' when that applies). 事業の内容 alone must
    reach MIN_SECTION_CHARS (the policy block never makes a too-short business section pass)."""
    business, policy, note = extract_business_blocks(zip_bytes)
    return (None if business is None else business + policy), note


def extract_business_blocks(zip_bytes: bytes) -> tuple[str | None, str, str]:
    """(事業の内容 text or None, policy block to append ('' or '\n\n<POLICY_HEADING>\n<text>'), note); see
    extract_business_section. The short description is computed from the first element only."""
    try:
        members = read_zip(zip_bytes)
    except (zipfile.BadZipFile, ValueError) as e:
        return None, "", f"bad_zip:{str(e)[:60] or type(e).__name__}"
    csvs = [(n, b) for n, b in members if n.lower().endswith(".csv")]
    if not csvs:
        return None, "", "zip_without_csv"
    csvs.sort(key=lambda nb: (0 if Path(nb[0]).name.lower().startswith("jpcrp") else
                              2 if Path(nb[0]).name.lower().startswith("jpaud") else 1, nb[0]))
    parsed: dict[str, tuple[list[list[str]], str] | None] = {}

    def rows_of(name: str, data: bytes) -> tuple[list[list[str]], str] | None:
        if name not in parsed:
            text, enc = decode_bytes(data, prefer=("utf-8", "cp932"))
            try:
                parsed[name] = (parse_xbrl_csv(text), enc)
            except csv.Error:
                parsed[name] = None
        return parsed[name]

    for name, data in csvs:
        got = rows_of(name, data)
        if got is None:
            continue
        rows, enc = got
        value = find_element_value(rows)
        if value is None:
            continue
        section = text_block_to_text(value)
        note = f"csv:{Path(name).name};enc:{enc}"
        blocks = ["business"]
        if len(section) > MAX_SECTION_CHARS:
            section = section[:MAX_SECTION_CHARS]
            note += ";capped"
        policy_value, policy_csv = find_element_value(rows, POLICY_BLOCK_ELEMENT), name
        if policy_value is None:
            for other, odata in csvs:
                if other == name:
                    continue
                ogot = rows_of(other, odata)
                policy_value = find_element_value(ogot[0], POLICY_BLOCK_ELEMENT) if ogot else None
                if policy_value is not None:
                    policy_csv = other
                    break
        extra = ""
        if policy_value is not None:
            ptext, capped = policy_text(policy_value)
            if re.search(r"\w", ptext):          # a lone '－' / heading-only block is not content
                blocks.append("policy")
                extra = f"\n\n{POLICY_HEADING}\n{ptext}"
                note += ";policy_capped" if capped else ""
                note += f";policy_csv:{Path(policy_csv).name}" if policy_csv != name else ""
        note += f";blocks:{','.join(blocks)}"
        if len(section) < MIN_SECTION_CHARS:            # 事業の内容 alone: the policy block does not count
            return None, "", f"section_too_short:{len(section)};{note}"
        return section, extra, note
    return None, "", "element_not_found"


def text_path_for(cfg: Config, edinet_code: str, doc_id: str) -> Path:
    return Path(cfg.home) / "docs" / "edinet" / edinet_code / f"{doc_id}-{SECTION}.txt"


# ============================================================================================ sync


def missing_key_message(cfg: Config) -> str:
    return ("EDINET API key missing: register for a free EDINET API v2 Subscription-Key "
            "(https://api.edinet-fsa.go.jp/api/auth/index.aspx?mode=1), then put it in "
            f"{Path(cfg.home) / KEY_FILE} (data/{KEY_FILE}, git-ignored) or set {KEY_ENV}.")


def sync(cfg: Config, client: Any = None, *, limit: int | None = None, codes: Iterable[str] | str | None = None,
         refresh: bool = False, min_mcap_usd: float | None = None, backfill_days: int = DEFAULT_BACKFILL_DAYS,
         batch_size: int = 25, progress_every: int = 100, only_universe: bool = True,
         as_of: dt.date | None = None, reuse_codelist_hours: float | None = None) -> dict:
    """Map JP universe lines to EDINET codes and pull each company's latest 有価証券報告書 事業の内容.

    reuse_codelist_hours (default: env JEVSCREEN_EDINET_REUSE_CODELIST_HOURS, else 0 = off): reuse a code list
    snapshot fetched less than that many hours ago (raw file present and matching its sha256) instead of
    downloading it again, e.g. right after `pack fetch-open`; refresh=True always downloads.

    Steps: (1) require cfg.edinet_api_key() (EdinetApiKeyMissing before any request); (2) GET the code list, save
    raw, snapshot, upsert identifiers ('edinet_code'), drop stale ones; (3) queue distinct EDINET codes by market
    cap desc (`codes` / `min_mcap_usd` / `limit` narrow it); (4) scan the daily lists newest first over
    `backfill_days` (final days cached on disk), stopping once every queued company has a report; (5) per company:
    skip when the stored document has the same docID and extractor (unless refresh), GET type=5, extract, write the
    text file; per batch one short session. Returns a summary whose 'status' is ok | blocked | stopped_errors |
    interrupted | store_locked | error. `client`: http.Client-compatible get(url, rate_key=, max_bytes=)."""
    key = cfg.edinet_api_key()
    if not key:
        raise EdinetApiKeyMissing(missing_key_message(cfg))
    redact = key_redactor(key, KEY_PLACEHOLDER)
    client = prepare_client(cfg, client, DEFAULT_MIN_INTERVAL_S)
    with guard.budget_lock(cfg, RATE_KEY, reentrant=True), sigterm_as_interrupt():
        return _sync(cfg, client, key, redact, limit=limit, codes=codes, refresh=refresh, min_mcap_usd=min_mcap_usd,
                     backfill_days=backfill_days, batch_size=batch_size, progress_every=progress_every,
                     only_universe=only_universe, as_of=as_of,
                     reuse_codelist_hours=_reuse_codelist_hours(reuse_codelist_hours))


REUSE_CODELIST_ENV = "JEVSCREEN_EDINET_REUSE_CODELIST_HOURS"


def _reuse_codelist_hours(v: float | None) -> float:
    if v is None:
        try:
            v = float(os.environ.get(REUSE_CODELIST_ENV) or 0)
        except ValueError:
            v = 0.0
    return max(0.0, min(float(v), 24.0))


def _recent_codelist(cfg: Config, hours: float) -> tuple[bytes, str] | None:
    """(raw body, snapshot_id) of an ok code-list snapshot fetched less than `hours` ago whose raw file still
    matches its recorded sha256, or None."""
    if hours <= 0 or not cfg.db_path.exists():
        return None
    since = store.now_utc() - dt.timedelta(hours=hours)
    with store.session(cfg, wait_s=FINAL_WAIT_S) as con:
        rows = con.execute(
            "SELECT raw_path, raw_sha256, snapshot_id FROM snapshots WHERE source_id = ? AND kind = 'edinet_codelist' "
            "AND raw_path IS NOT NULL AND coalesce(status, 'ok') = 'ok' AND fetched_at >= ? "
            "ORDER BY fetched_at DESC LIMIT 3", [SOURCE_ID, since]).fetchall()
    for path, digest, snap in rows:
        try:
            body = Path(path).read_bytes()
        except OSError:
            continue
        if digest and store.sha256(body) == digest:
            return body, snap
    return None


def _sync(cfg: Config, client: Any, key: str, redact, *, limit, codes, refresh, min_mcap_usd, backfill_days,
          batch_size, progress_every, only_universe, as_of, reuse_codelist_hours: float = 0.0) -> dict:
    run = SyncRun(cfg, source_id=SOURCE_ID, command=COMMAND, client=client, rate_key=RATE_KEY, redact=redact,
                  key_placeholder=KEY_PLACEHOLDER, log_tag="edinet", statuses=CRAWL_STATUSES,
                  batch_size=batch_size, progress_every=progress_every)
    summary = run.summary
    summary.update({"codelist_rows": 0, "list_days_scanned": 0, "list_days_cached": 0, "list_days_fetched": 0,
                    "list_days_failed": 0, "list_scan_stopped_early": False, "ok_documents": 0})
    backfill_days = max(1, min(int(backfill_days), MAX_BACKFILL_DAYS))
    today = as_of or dt.datetime.now(JST).date()
    extractor = f"{EXTRACTOR_VERSION}/csv"
    run.start()

    # ---- (2) code list (network first, then one short session) -------------------------------------------
    t0 = time.monotonic()
    fetched_at = store.now_utc()
    cached = None if refresh else _recent_codelist(cfg, reuse_codelist_hours)
    if cached is not None:
        try:
            entries = parse_codelist(cached[0])
        except (ValueError, zipfile.BadZipFile, csv.Error):
            entries = []
        if not entries:
            cached = None
    summary["codelist_reused"] = cached is not None
    try:
        resp = None if cached is not None else run.fetch(CODELIST_URL, max_bytes=MAX_CODELIST_BYTES,
                                                         deadline_s=CODELIST_DEADLINE_S)
    except Blocked as e:
        run.mark_blocked(CODELIST_URL, e.status, e.reason)
        summary.update(stopped_reason="blocked", blocked_at=CODELIST_URL)
        return run.close(f"codelist: {e.reason}")
    except KeyboardInterrupt:
        summary["stopped_reason"] = "interrupted"
        return run.close()
    except (OSError, http.client.HTTPException) as e:
        summary["stopped_reason"] = "codelist_error"
        return run.close(err_note(redact, "codelist: ", e))
    if resp is not None:
        if resp.status != 200:
            summary["stopped_reason"] = "codelist_error"
            return run.close(f"codelist: http_{resp.status}")
        try:
            entries = parse_codelist(resp.body)
        except (ValueError, zipfile.BadZipFile, csv.Error) as e:
            summary["stopped_reason"] = "codelist_error"
            return run.close(err_note(redact, "codelist: ", e))
        if not entries:
            summary["stopped_reason"] = "codelist_error"
            return run.close("codelist: empty")
    summary["codelist_rows"] = len(entries)
    report: dict = {}
    try:
        if resp is not None:
            raw_path, digest = store.save_raw(cfg, SOURCE_ID, "edinet_codelist", resp.body, suffix="zip")
        with store.session(cfg, wait_s=FINAL_WAIT_S) as con:
            if resp is None:
                snap = cached[1]          # the recent snapshot the code list came from
            else:
                snap = store.record_snapshot(
                    con, source_id=SOURCE_ID, kind="edinet_codelist", request=run.request_record(CODELIST_URL),
                    raw_path=raw_path, raw_sha256=digest, raw_bytes=len(resp.body), rows=len(entries),
                    duration_s=round(time.monotonic() - t0, 3), fetched_at=fetched_at)
                summary["snapshots"] += 1
            lines = filter_lines(load_lines(con, JP_VENUES, only_universe=only_universe), codes, min_mcap_usd)
            mapping = map_securities_to_edinet(lines, entries, report=report)
            run.write_mapping(con, lines, mapping, ID_TYPE, snap, report)
            stored = stored_documents(con, SOURCE_ID)
            run.attempts = dict(con.execute("SELECT security_id, attempts FROM crawl_state WHERE source_id = ?",
                                            [SOURCE_ID]).fetchall())
    except KeyboardInterrupt:
        summary["stopped_reason"] = "interrupted"
        return run.close()

    # ---- (3) queue --------------------------------------------------------------------------------------
    queue = run.queue_from_mapping(lines, mapping, limit)
    targets = set(queue)

    # ---- (4) daily list scan, newest day first ----------------------------------------------------------
    found: dict[str, dict] = {}
    other_annual: dict[str, list[str]] = {}   # code -> ['ord/form' | 'withdrawn'] of unusable 120 filings
    # docIDs seen withdrawn / 不開示 on a newer day: an older (cached) list still shows them as usable
    unusable_ids: set[str] = set()
    scan_manifest: list[dict] = []
    fetched_urls: list[str] = []
    consecutive = 0
    days_scanned = 0

    def load_day(day: dt.date) -> list[dict] | None:
        """Rows of one day's list (cache first). None after a per-day failure (recorded in the manifest)."""
        path = list_cache_path(cfg, day)
        old = outside_retention(day, today)
        if path.exists():
            try:
                rows = parse_documents_list(path.read_bytes())
                summary["list_days_cached"] += 1
                scan_manifest.append({"date": day.isoformat(), "source": "cache", "rows": len(rows)})
                return rows
            except (ValueError, EdinetStatusError, ApiStop):
                pass                       # unreadable cache: fetch again
        public = LIST_URL.format(date=day.isoformat())
        try:
            resp = run.fetch(with_key(public, KEY_PARAM, key), deadline_s=LIST_DEADLINE_S)
        except RequestTimeout as e:         # a stalled list: a per-day failure (the consecutive-error stop applies)
            summary["list_days_fetched"] += 1
            fetched_urls.append(public)
            scan_manifest.append({"date": day.isoformat(), "source": "api", "error": deadline_note(e)})
            return None
        summary["list_days_fetched"] += 1
        fetched_urls.append(public)
        if resp.status == 401:
            raise ApiStop("key_rejected", 401)
        if resp.status in (400, 404):
            if old:                        # outside the retained range: no list
                scan_manifest.append({"date": day.isoformat(), "source": "api", "http_status": resp.status,
                                      "rows": 0})
                return []
            # inside the range a 400/404 means a wrong endpoint / parameter: a failure, so the error stop applies
            scan_manifest.append({"date": day.isoformat(), "source": "api", "error": f"http_{resp.status}"})
            return None
        if resp.status != 200:
            scan_manifest.append({"date": day.isoformat(), "source": "api", "error": f"http_{resp.status}"})
            return None
        try:
            payload = json.loads(resp.body)
            rows = parse_documents_list(payload)
        except ValueError:
            scan_manifest.append({"date": day.isoformat(), "source": "api", "error": "bad_json"})
            return None
        except EdinetStatusError as e:
            if old and e.status in ("400", "404"):
                scan_manifest.append({"date": day.isoformat(), "source": "api", "status": e.status, "rows": 0})
                return []
            scan_manifest.append({"date": day.isoformat(), "source": "api", "error": f"status_{e.status}"})
            return None
        body = redact(resp.body)
        entry = {"date": day.isoformat(), "source": "api", "rows": len(rows), "sha256": store.sha256(body)}
        if (today - day).days >= FINAL_AFTER_DAYS:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(body)
            os.replace(tmp, path)
            entry["cached"] = str(path)
        scan_manifest.append(entry)
        return rows

    scan_error: str | None = None
    try:
        for i in range(backfill_days if targets else 0):
            if targets <= found.keys():
                summary["list_scan_stopped_early"] = True
                break
            day = today - dt.timedelta(days=i)
            rows = load_day(day)
            days_scanned += 1
            if rows is None:
                summary["list_days_failed"] += 1
                consecutive += 1
                if consecutive >= MAX_CONSECUTIVE_ERRORS:
                    summary["stopped_reason"] = "list_errors"
                    break
                continue
            consecutive = 0
            day_filings = []
            for r in rows:
                if is_unusable_copy(r) and r.get("docID"):
                    unusable_ids.add(str(r["docID"]))
            for r in rows:
                code = str(r.get("edinetCode") or "").upper()
                if code not in targets or code in found:
                    continue
                if is_annual_report(r) and str(r.get("docID")) in unusable_ids:
                    if "withdrawn" not in other_annual.setdefault(code, []):
                        other_annual[code].append("withdrawn")
                    continue
                if is_annual_report(r):
                    day_filings.append(_filing_from_row(r, day))
                elif str(r.get("docTypeCode") or "") == ANNUAL_DOC_TYPE:
                    label = ("withdrawn" if is_unusable_copy(r)
                             else f"{r.get('ordinanceCode')}/{r.get('formCode')}")
                    if label not in other_annual.setdefault(code, []):
                        other_annual[code].append(label)
            found.update(pick_latest(day_filings))
            if progress_every and summary["list_days_fetched"] and summary["list_days_fetched"] % 25 == 0 \
                    and rows is not None:
                run.log(f"lists: {days_scanned}/{backfill_days} days, {len(found)}/{len(targets)} companies found")
    except Blocked as e:
        run.mark_blocked(LIST_URL.format(date=(today - dt.timedelta(days=days_scanned)).isoformat()),
                         e.status, e.reason)
        summary.update(stopped_reason="blocked", blocked_at=f"list:{today - dt.timedelta(days=days_scanned)}")
        scan_error = f"lists: {e.reason}"
    except ApiStop as e:
        if e.cooldown:
            run.mark_blocked(LIST_URL.format(date=(today - dt.timedelta(days=days_scanned)).isoformat()),
                             e.status, e.reason)
        summary.update(stopped_reason=e.reason, blocked_at=f"list:{today - dt.timedelta(days=days_scanned)}",
                       stop_message=redact(e.message)[:300] if e.message else None)
        scan_error = f"lists: {e.reason}"
    except KeyboardInterrupt:
        summary["stopped_reason"] = "interrupted"
    except (OSError, http.client.HTTPException) as e:
        summary["stopped_reason"] = "list_errors"
        scan_error = err_note(redact, "lists: network: ", e)
    summary["list_days_scanned"] = days_scanned
    if scan_manifest:
        manifest = json.dumps({"from": today.isoformat(), "days": scan_manifest}, sort_keys=True).encode()
        run.add_aux("documents_list_scan", LIST_URL.format(date=today.isoformat()), manifest, store.now_utc(),
                    urls_fetched=len(fetched_urls))
    if summary["stopped_reason"]:
        return run.close(scan_error)
    run.log(f"lists: scanned {days_scanned} days ({summary['list_days_cached']} cached, "
            f"{summary['list_days_fetched']} fetched); {len(found)}/{len(targets)} companies have a report")

    # ---- (5) documents ----------------------------------------------------------------------------------
    failed_days = summary["list_days_failed"]

    def process(code: str) -> str:
        at = store.now_utc()
        filing = found.get(code)
        if filing is None:
            note = f"no_yuho_in_last_{days_scanned}_days"
            if code in other_annual:
                note += f";other_annual:{','.join(other_annual[code])}"
            if failed_days:
                note += f";list_days_failed:{failed_days}"
            run.state(code, "no_annual_filing", None, note, at)
            return "no_annual_filing"
        doc_id = filing["doc_id"]
        if not refresh and stored.get(code) == (doc_id, extractor):
            summary["skipped_unchanged"] += 1
            return "skipped"
        if filing.get("csv_flag") == "0":
            run.state(code, "no_csv", None, f"csv_not_provided:{doc_id}", at)
            return "no_csv"
        public = DOC_URL.format(doc_id=doc_id)
        resp = run.fetch(with_key(public, KEY_PARAM, key), max_bytes=MAX_DOC_BYTES, deadline_s=DOC_DEADLINE_S)
        if resp.status == 401:
            raise ApiStop("key_rejected", 401)
        if resp.status != 200:
            run.state(code, "error", resp.status, f"document_http_{resp.status}", at)
            return "error"
        raw = resp.body or b""
        if not is_zip(raw):
            try:
                check_status(json.loads(raw[:1 << 20]))
                note = "document_not_zip"
            except EdinetStatusError as e:
                note = f"document_status_{e.status}"
            except ValueError:
                note = "document_not_zip"
            run.state(code, "error", resp.status, note, at)
            return "error"
        business, policy_extra, xnote = extract_business_blocks(raw)
        section = None if business is None else business + policy_extra
        if getattr(resp, "truncated", False):
            xnote += ";truncated"
        flag = stale_flag(date_or_none(filing.get("period_end")) or date_or_none(filing.get("submit_datetime")),
                          today)
        if flag:
            xnote += f";{flag}"
        primary = run.groups[code][0]
        row = {"doc_id": f"{SOURCE_ID}:{code}:{doc_id}:{SECTION}", "security_id": primary["security_id"],
               "company_key": primary["company_key"], "source_id": SOURCE_ID, "cik": None, "form": FORM_LABEL,
               "section": SECTION, "accession": doc_id, "filing_date": date_or_none(filing.get("submit_datetime")),
               "report_date": date_or_none(filing.get("period_end")), "url": public,
               "raw_sha256": store.sha256(raw), "raw_bytes": len(raw), "text_path": None, "text_sha256": None,
               "text_chars": None, "extractor": extractor, "extract_note": redact(xnote), "fetched_at": at}
        if section is None:
            run.batch.docs.append(row)
            run.state(code, "extract_failed", 200, xnote, at)
            return "extract_failed"
        tp = text_path_for(cfg, code, doc_id)
        tp.parent.mkdir(parents=True, exist_ok=True)
        data = section.encode("utf-8")
        tp.write_bytes(data)
        row.update(text_path=str(tp), text_sha256=store.sha256(data), text_chars=len(section))
        run.batch.docs.append(row)
        desc = short_description_cjk(business)          # 事業の内容 only, never the 経営方針 block
        if desc:
            run.add_description(code, desc, "ja", public, at, ID_TYPE)
        run.state(code, "ok", 200, xnote, at)
        summary["ok_documents"] += 1
        return "ok"

    def url_for(code: str) -> str:
        f = found.get(code)
        return DOC_URL.format(doc_id=f["doc_id"]) if f else LIST_URL.format(date=today.isoformat())

    run.run_queue(queue, process, url_for)
    return run.close()
