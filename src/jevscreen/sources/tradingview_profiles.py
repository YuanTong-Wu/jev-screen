"""Business descriptions from TradingView symbol pages (source_id 'tradingview_profile', licence gray-private).

Data rules implemented here (docs/DATA_RULES.md, "Descriptions"):
- Page: https://www.tradingview.com/symbols/{EXCHANGE}-{SYMBOL}/ ; the description is the JSON-LD `FinancialProduct`
  object. JSON-LD is parsed with html.parser + json; page JavaScript is never executed.
- At most MAX_RATE = 4.4 requests/second (measured ceiling; faster rates are clamped). Pages are STREAMED with
  http.Client.get_until in 16 KB chunks; ProductDetector parses application/ld+json scripts incrementally and the
  connection is closed as soon as one complete FinancialProduct with a non-empty description has been read (a live
  probe on 2026-09-26 found it ending at ~89-90 KB on every page). A complete FinancialProduct WITHOUT an acceptable
  description also ends the stream and is the terminal 'no_description'. MAX_BYTES = 256 KB is only the hard cap: a
  page cut at the cap before any complete FinancialProduct gets the soft, retryable status 'no_product_within_cap',
  never 'no_description', and does not count toward the consecutive-error stop.
- Worker pool: `workers` threads (default 3, clamped to 1..4) overlap the ~0.47 s page downloads. The shared
  http.Client limiter is thread-safe, so request starts stay >= 1/4.4 s apart whatever the worker count, and
  apply_rate also caps them at MAX_STARTS_PER_WINDOW = 4 in any rolling second (sustained <= 4.0/s: a margin under
  the 4.4/s ceiling for connection/handshake jitter between the limiter slot and what the server sees). Only a
  client that declares thread_safe = True (http.Client) gets more than one worker. Workers only fetch + parse; the
  main thread owns the queue, the covered-set check (just before each submit), the batch, the flushes, the counters,
  the progress lines and every stop decision, and records results in completion order. Each page has its own
  Future, registered before submit, so no result can be lost between threads.
- One process per rate budget: crawl() takes guard.budget_lock('www.tradingview.com', reentrant=True), so a script
  or notebook cannot run next to a CLI crawl with a second limiter (the CLI already holds it and re-enters).
- Companies already covered by an SEC annual-report business section (a 'sec_filing_text' description, or a
  documents row with text whose cik matches the line's identifiers 'sec_cik') are dropped before crawling. Because
  sync-sec may be running at the same time, the covered set is re-read at every flush and newly covered companies are
  skipped before they are fetched.
- The FIRST HTTP 403/429 or challenge (raised by http.Client as Blocked) stops the WHOLE run immediately: no retry,
  no slowing down. Order: (1) http.Client sets its halt Event in the same step that observes the block, so no
  further request starts anywhere (a caller already waiting in the limiter gets Halted and sends nothing); (2) the
  worker thread that got it writes the cooldown marker (guard.mark_blocked) right away, before the main thread
  handles the result or waits for the database; (3) the main thread records crawl_state 'blocked' and calls
  `on_blocked(exc)` (optional; the CLI uses it to ignore SIGTERM) as soon as it handles the result, which can be
  delayed by a mid-run flush waiting for the store lock; (4) requests already in flight (at most workers - 1)
  finish and are recorded normally (drain bounded by DRAIN_MAX_S); (5) final flush; the summary carries blocked_url /
  blocked_status / blocked_reason so the CLI can print its BLOCKED line and exit 2. If the final write after a block
  fails, the summary is still returned (with flush_error) so the BLOCKED line and exit 2 are never lost. Within
  COOLDOWN_HOURS of a recorded block crawl() refuses to start (CooldownActive) unless after_block=True. Bot
  protection is never bypassed.
- 3 consecutive errors (network, 5xx, unexpected status), counted in completion order, stop the run (halt, drain).
  SIGTERM / Ctrl-C: halt, drain the in-flight requests (at most DRAIN_MAX_S; a second Ctrl-C stops waiting), flush,
  'interrupted'. Abandoned requests stay in the queue; the interpreter still joins their threads at exit, each
  socket operation bounded by the client's timeout (15 s for the CLI crawl client). Ticker/URL validation mismatches
  ('mismatch'), redirects ('redirect') and 'no_product_within_cap' do not count; they are retried in later runs until
  they reach MAX_SOFT_ATTEMPTS attempts.
- Resumable: crawl_state per (source, security); ok / not_found / no_description are terminal and skipped later.
- Raw pages (~500 KB) are NOT stored. Only the extracted FinancialProduct objects are kept, as one JSONL raw file and
  one snapshot per batch (kind 'profile_batch'); each batch is written in one transaction, so an interrupted run
  loses at most one batch and resumes cleanly.
- Short sessions: no DuckDB connection is held across a fetch (the main thread's short flush sessions may overlap
  fetches running in worker threads; workers never touch the store). Each flush (every `flush_every` pages and at
  the end/stop/interrupt) opens store.session, writes, and closes, so coverage/status stay usable during a crawl.
- Missing values stay NULL.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import http.client
import itertools
import json
import math
import re
import signal
import sys
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor
from concurrent.futures import wait as futures_wait
from html.parser import HTMLParser
from typing import Any, Callable, Iterator
from urllib.parse import quote, unquote, urlparse

from .. import guard, store
from ..config import Config
from ..http import Blocked, Halted

SOURCE_ID = "tradingview_profile"
BASE_URL = "https://www.tradingview.com"
SNAPSHOT_KIND = "profile_batch"
MAX_DESCRIPTION_CHARS = 20_000
MAX_BYTES = 256 * 1024      # hard cap per page (the FinancialProduct block ends at ~90 KB)
CHUNK_BYTES = 16 * 1024     # streaming read size
MAX_RATE = 4.4              # requests/second: measured ceiling, default and hard maximum
MAX_RATE_PER_S = MAX_RATE   # backward-compatible name
MAX_BATCH = 500
TERMINAL_STATUSES = ("ok", "not_found", "no_description")
# retried later, but only up to MAX_SOFT_ATTEMPTS attempts; never counted toward the consecutive-error stop
SOFT_STATUSES = ("mismatch", "redirect", "no_product_within_cap")
SEC_SOURCE_ID = "sec_filing_text"
SEC_ID_TYPE = "sec_cik"
MAX_SOFT_ATTEMPTS = 3
DEFAULT_WORKERS = 3          # fetch threads; the shared client limiter still caps starts at MAX_RATE
MAX_WORKERS = 4
RESULT_POLL_S = 0.5          # main thread wakes at least this often while waiting for a worker result
DRAIN_MAX_S = 30.0           # after a stop, wait at most this long for in-flight requests before the final flush
COOLDOWN_HOURS = 24          # crawl() refuses to start within this long of a recorded block (after_block overrides)
MAX_STARTS_PER_WINDOW = 4    # never more than floor(MAX_RATE) request starts in any rolling 1 s (jitter margin)


# --------------------------------------------------------------------------- URL


def profile_url(security_id: str) -> str:
    """'EXCHANGE:SYMBOL' -> 'https://www.tradingview.com/symbols/EXCHANGE-SYMBOL/' (e.g. NYSE:BRK.B -> NYSE-BRK.B).

    Both parts are percent-quoted; '.', '_', '-', '!' stay literal as TradingView uses them. '/' is quoted so a
    symbol can never escape the /symbols/ path.
    """
    exchange, sep, symbol = (security_id or "").partition(":")
    if not sep or not exchange.strip() or not symbol.strip():
        raise ValueError(f"security_id must be 'EXCHANGE:SYMBOL', got {security_id!r}")
    safe = "._-!"
    return f"{BASE_URL}/symbols/{quote(exchange.strip(), safe=safe)}-{quote(symbol.strip(), safe=safe)}/"


def clamp_rate(rate_per_s: float | None) -> float:
    """Requests/second allowed for this crawler: default MAX_RATE (4.4), never above it."""
    if rate_per_s is None:
        return MAX_RATE_PER_S
    rate = float(rate_per_s)
    if math.isnan(rate) or rate <= 0:
        raise ValueError("rate must be a number > 0 requests/second")
    return min(rate, MAX_RATE_PER_S)   # +inf and anything above the ceiling -> MAX_RATE


def apply_rate(client: Any, rate_per_s: float | None = None) -> float:
    """Configure a shared http.Client for this crawler: clamp the rate by raising min_interval_s (never lowering it)
    and, on a client with a window limiter (http.Client.max_per_window), allow at most MAX_STARTS_PER_WINDOW = 4
    starts in any rolling second (never raising an existing lower cap).

    Returns the effective rate. The read cap is passed per request, so the client's own max_bytes is left alone.
    """
    floor = 1.0 / clamp_rate(rate_per_s)
    try:
        current = float(getattr(client, "min_interval_s", 0.0) or 0.0)
    except (TypeError, ValueError):
        current = 0.0
    # NaN-safe: `not (current >= floor)` also catches NaN, which would otherwise switch the limiter off
    client.min_interval_s = current if (current >= floor and math.isfinite(current)) else floor
    if hasattr(client, "max_per_window"):   # http.Client: also never more than 4 starts in any rolling second
        try:
            window_cap = int(client.max_per_window or 0)
        except (TypeError, ValueError):
            window_cap = 0
        client.max_per_window = min(window_cap, MAX_STARTS_PER_WINDOW) if window_cap > 0 else MAX_STARTS_PER_WINDOW
        try:
            window = float(getattr(client, "window_s", 1.0))
        except (TypeError, ValueError):
            window = 1.0
        client.window_s = window if (window >= 1.0 and math.isfinite(window)) else 1.0   # never a shorter window
    return 1.0 / client.min_interval_s


# --------------------------------------------------------------------------- extraction

_ATTR = rb"""(?:[^>"']|"[^"]*"|'[^']*')"""   # one char of a start tag, or a whole quoted value (may hold '>')
_LDJSON_OPEN = re.compile(rb"<script\b" + _ATTR + rb"*?(?<![\w-])type\s*=\s*([\"']?)\s*application/ld\+json\s*"
                          rb"(?:;[^\"'>]*)?\1" + _ATTR + rb"*>", re.I)
_SCRIPT_CLOSE = re.compile(rb"</script\s*>", re.I)
_SCRIPT_START = re.compile(rb"<script", re.I)


def _is_ldjson_type(value: str | None) -> bool:
    """'application/ld+json', case-insensitive, parameters (';charset=...') ignored; same rule as _LDJSON_OPEN."""
    return (value or "").split(";", 1)[0].strip().lower() == "application/ld+json"


class ProductDetector:
    """Incremental stop() for http.Client.get_until: True once a COMPLETE <script type="application/ld+json"> block
    containing a FinancialProduct has been read.

    A FinancialProduct with an acceptable description is kept in `.product`; one without (empty, missing or too long)
    in `.bare_product` (the page then has no usable description: terminal 'no_description'). Either one stops the
    stream. Each call resumes where the previous one stopped, so every script block is parsed once; incomplete blocks
    are re-examined when more bytes arrive. Nothing is ever executed.
    """

    def __init__(self) -> None:
        self.pos = 0
        self.product: dict | None = None
        self.bare_product: dict | None = None

    @property
    def done(self) -> bool:
        return self.product is not None or self.bare_product is not None

    def __call__(self, buffer: bytes) -> bool:
        if self.done:
            return True
        while True:
            m = _LDJSON_OPEN.search(buffer, self.pos)
            if m is None:
                # resume at the last '<script' (its start tag may be split across chunks, however long it is);
                # without one, keep a short tail in case '<script' itself is split
                last = None
                for last in _SCRIPT_START.finditer(buffer, self.pos):
                    pass
                self.pos = last.start() if last is not None else max(self.pos, len(buffer) - 8)
                return False
            end = _SCRIPT_CLOSE.search(buffer, m.end())
            if end is None:
                self.pos = m.start()           # block not complete yet: resume here next time
                return False
            self.pos = end.end()
            block = buffer[m.end():end.start()]
            if b"FinancialProduct" not in block:
                continue
            try:
                value = json.loads(block.decode("utf-8", "replace"))
            except (ValueError, TypeError):
                continue
            good, bare = _pick(value)
            if good is not None:
                self.product = good
                return True
            if bare is not None:
                self.bare_product = bare
                return True


def product_complete(buffer: bytes) -> bool:
    """True when `buffer` holds a complete ld+json script with a FinancialProduct that has a description."""
    det = ProductDetector()
    det(buffer)
    return det.product is not None



class _JsonLdParser(HTMLParser):
    """Collects the text of <script type="application/ld+json"> blocks. Never executes anything."""

    def __init__(self) -> None:
        super().__init__()
        self.blocks: list[str] = []
        self._parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and _is_ldjson_type(dict(attrs).get("type")):
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._parts is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._parts is not None:
            self.blocks.append("".join(self._parts))
            self._parts = None


def _walk(value: Any) -> Iterator[dict]:
    """Yield every dict in a JSON-LD value (covers @graph, lists and nesting)."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _is_financial_product(obj: dict) -> bool:
    t = obj.get("@type")
    return t == "FinancialProduct" or (isinstance(t, list) and "FinancialProduct" in t)


def _acceptable(description: Any) -> bool:
    return isinstance(description, str) and bool(description.strip()) and len(description) <= MAX_DESCRIPTION_CHARS


def _pick(value: Any) -> tuple[dict | None, dict | None]:
    """(first FinancialProduct with an acceptable description, first FinancialProduct at all) in one JSON value."""
    bare = None
    for obj in _walk(value):
        if _is_financial_product(obj):
            if _acceptable(obj.get("description")):
                return obj, bare or obj
            bare = bare or obj
    return None, bare


def find_financial_products(html: str) -> tuple[dict | None, dict | None]:
    """(first complete FinancialProduct with an acceptable description, first complete FinancialProduct at all)."""
    parser = _JsonLdParser()
    parser.feed(html)
    parser.close()
    bare = None
    for block in parser.blocks:
        try:
            value = json.loads(block)
        except (ValueError, TypeError):
            continue
        good, any_fp = _pick(value)
        if good is not None:
            return good, bare or any_fp
        bare = bare or any_fp
    return None, bare


def find_financial_product(html: str) -> dict | None:
    """Return the first complete JSON-LD FinancialProduct object with an acceptable description, else None.

    Acceptable = non-empty string of at most 20,000 characters. Malformed JSON-LD blocks are ignored.
    """
    return find_financial_products(html)[0]


def extract_description(html: str) -> dict | None:
    """{name, tickerSymbol, description, url} from the page's JSON-LD FinancialProduct, or None if absent/rejected.

    Absent keys stay None (never empty-string or 0). The description is kept verbatim.
    """
    obj = find_financial_product(html)
    if obj is None:
        return None
    return {k: obj.get(k) for k in ("name", "tickerSymbol", "description", "url")}


def _norm_ticker(value: str) -> str:
    """Case-insensitive ticker compare that tolerates TradingView's formatting (BRK.B / BRK-B / BRK/B / 'NYSE:BRK.B')."""
    value = value.strip()
    if ":" in value:
        value = value.split(":", 1)[1]
    return re.sub(r"[^0-9a-z]", "", value.casefold())


def _norm_path(url: str) -> str:
    return unquote(urlparse(url).path).rstrip("/").casefold()


def validate(product: dict, security_id: str, requested_url: str) -> str | None:
    """Bind the extracted object to the requested security. Returns a mismatch reason, or None when it matches.

    - tickerSymbol must equal the SYMBOL part of security_id (case/punctuation-insensitive).
    - url, if present, must point to tradingview.com and have the same path as the requested URL.
    """
    symbol = security_id.partition(":")[2]
    ticker = product.get("tickerSymbol")
    if not isinstance(ticker, str) or not ticker.strip():
        return "ticker_missing"
    if _norm_ticker(ticker) != _norm_ticker(symbol):
        return f"ticker_mismatch: page={ticker!r} expected={symbol!r}"
    url = product.get("url")
    if url:
        if not isinstance(url, str):
            return "url_invalid"
        host = urlparse(url).netloc.lower()
        if host and host != "tradingview.com" and not host.endswith(".tradingview.com"):
            return f"url_host_mismatch: {url}"
        if _norm_path(url) != _norm_path(requested_url):
            return f"url_path_mismatch: page={url} requested={requested_url}"
    return None


# --------------------------------------------------------------------------- queue


def sec_covered(con) -> set[str]:
    """company_keys and security_ids of universe companies already covered by an SEC annual-report business section:
    a 'sec_filing_text' description (by security_id or company_key), or a documents row with non-null text_path whose
    cik equals an identifiers 'sec_cik' value of one of the company's lines (leading zeros ignored)."""
    rows = con.execute("""
        WITH covered AS (
            SELECT s.company_key FROM securities s
            JOIN descriptions d ON d.security_id = s.security_id
            WHERE d.source_id = ?
            UNION
            SELECT d.company_key FROM descriptions d WHERE d.source_id = ? AND d.company_key IS NOT NULL
            UNION
            SELECT s.company_key FROM securities s
            JOIN identifiers i ON i.security_id = s.security_id AND i.id_type = ?
            JOIN documents doc ON ltrim(CAST(doc.cik AS VARCHAR), '0') = ltrim(CAST(i.id_value AS VARCHAR), '0')
            WHERE doc.text_path IS NOT NULL)
        SELECT s.security_id, s.company_key FROM securities s WHERE s.company_key IN (SELECT company_key FROM covered)
    """, [SEC_SOURCE_ID, SEC_SOURCE_ID, SEC_ID_TYPE]).fetchall()
    out: set[str] = set()
    for sid, ck in rows:
        out.add(sid)
        if ck:
            out.add(ck)
    return out


def _is_covered(covered: set[str], sid: str, company_key: str | None) -> bool:
    return sid in covered or (company_key is not None and company_key in covered)


def _queue_rows(con, *, only_missing: bool = True, limit: int | None = None, skip_sec_covered: bool = True,
                covered: set[str] | None = None, countries: list[str] | None = None,
                min_mcap_usd: float | None = None) -> list[tuple[str, str | None]]:
    sql = f"""
        SELECT u.security_id, u.company_key, u.country, u.exchange FROM universe u
        LEFT JOIN crawl_state c ON c.source_id = ? AND c.security_id = u.security_id
        WHERE (c.status IS NULL OR c.status NOT IN ({",".join("?" for _ in TERMINAL_STATUSES)}))
          AND NOT (coalesce(c.status IN ({",".join("?" for _ in SOFT_STATUSES)}), false)
                   AND coalesce(c.attempts, 0) >= ?)
          AND (NOT ? OR NOT EXISTS (
                SELECT 1 FROM descriptions d
                WHERE d.company_key = u.company_key
                   OR d.security_id IN (SELECT s.security_id FROM securities s WHERE s.company_key = u.company_key)))
          {"AND u.market_cap_usd IS NOT NULL AND u.market_cap_usd >= ?" if min_mcap_usd is not None else ""}
        ORDER BY u.market_cap_usd DESC NULLS LAST, u.security_id
    """
    params: list[Any] = [SOURCE_ID, *TERMINAL_STATUSES, *SOFT_STATUSES, MAX_SOFT_ATTEMPTS, bool(only_missing)]
    if min_mcap_usd is not None:
        params.append(float(min_mcap_usd))
    fetched = con.execute(sql, params).fetchall()
    if countries:
        from ..screen import country_matcher   # ISO-2 codes, country names or regions (ValueError on a typo)
        keep = country_matcher(countries, {r[2] for r in fetched})
        fetched = [r for r in fetched if keep(r[2], r[3])]
    rows = [(r[0], r[1]) for r in fetched]
    if skip_sec_covered:
        covered = sec_covered(con) if covered is None else covered
        rows = [r for r in rows if not _is_covered(covered, r[0], r[1])]
    return rows if limit is None else rows[:max(0, int(limit))]


def queue(con, *, only_missing: bool = True, limit: int | None = None, skip_sec_covered: bool = True) -> list[str]:
    """Securities to crawl: the universe view's chosen line per company, largest USD market cap first.

    Skips terminal crawl_state (ok / not_found / no_description) for this source; 'blocked' and 'error' are retried
    in later runs, soft statuses until MAX_SOFT_ATTEMPTS attempts. With only_missing, also skips companies that
    already have a description from ANY source; with skip_sec_covered (default), companies covered by an SEC
    annual-report business section (sec_covered) are always dropped.
    """
    return [sid for sid, _ in _queue_rows(con, only_missing=only_missing, limit=limit,
                                          skip_sec_covered=skip_sec_covered)]


# --------------------------------------------------------------------------- crawl


class _Batch:
    """Pending writes for up to batch_size pages; flushed in one transaction with one snapshot."""

    def __init__(self) -> None:
        self.lines: list[dict] = []
        self.descriptions: list[dict] = []   # rows waiting for the batch snapshot_id
        self.states: list[list[Any]] = []
        self.requested: list[str] = []
        self.started_at: dt.datetime | None = None
        self.duration_s = 0.0

    def __len__(self) -> int:
        return len(self.states)


def _flush(cfg: Config, con, batch: _Batch, attempts: dict[str, int], note: str | None = None) -> str | None:
    """Write raw JSONL + snapshot + descriptions + crawl_state atomically. Returns the snapshot_id (or None)."""
    if not len(batch):
        return None
    # one row per security (the last one wins): a result handled twice after an interrupt never makes a duplicate
    # key inside one upsert
    states = list({row[1]: row for row in batch.states}.values())
    descriptions = list({d["security_id"]: d for d in batch.descriptions}.values())
    snapshot_id = None
    con.begin()
    try:
        if batch.lines:
            raw = "".join(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n" for line in batch.lines).encode()
            path, digest = store.save_raw(cfg, SOURCE_ID, SNAPSHOT_KIND, raw, suffix="jsonl")
            snapshot_id = store.record_snapshot(
                con, source_id=SOURCE_ID, kind=SNAPSHOT_KIND,
                request={"method": "GET", "urls": batch.requested}, raw_path=path, raw_sha256=digest,
                raw_bytes=len(raw), rows=len(batch.lines), duration_s=round(batch.duration_s, 3),
                note=note, fetched_at=batch.started_at)
        store.upsert_many(con, "descriptions",
                          ["security_id", "source_id", "company_key", "text", "text_sha256", "lang", "source_url",
                           "fetched_at", "match_method", "match_score", "snapshot_id"],
                          [[d["security_id"], SOURCE_ID, d["company_key"], d["text"],
                            store.sha256(d["text"].encode()), "en", d["url"], d["fetched_at"],
                            "tv_symbol_page_jsonld", 1.0, snapshot_id] for d in descriptions])
        store.upsert_many(con, "crawl_state",
                          ["source_id", "security_id", "status", "http_status", "attempts", "last_attempt_at", "note"],
                          states)
        con.commit()
    except BaseException:
        con.rollback()
        raise
    for row in states:
        attempts[row[1]] = row[4]
    return snapshot_id


def _fetch(client: Any, url: str) -> tuple[Any, ProductDetector]:
    """Stream the page and stop at the FinancialProduct (get_until); plain get() for clients without it."""
    detector = ProductDetector()
    get_until = getattr(client, "get_until", None)
    if callable(get_until):
        return get_until(url, detector, max_bytes=MAX_BYTES, chunk_size=CHUNK_BYTES), detector
    return client.get(url, max_bytes=MAX_BYTES), detector


def _run_page(client: Any, sid: str, url: str, meter: dict | None = None
              ) -> tuple[str, int | None, str | None, dict | None]:
    """Fetch and classify one symbol page -> (status, http_status, note, product). Blocked propagates.

    `meter` (optional) accumulates pages / bytes_read / early_stops."""
    try:
        resp, detector = _fetch(client, url)
    except (OSError, TimeoutError, http.client.HTTPException) as e:  # URLError is an OSError; IncompleteRead
        return "error", None, f"network: {type(e).__name__}: {str(e)[:200]}", None
    if meter is not None:
        meter["pages"] += 1
        meter["bytes_read"] += len(resp.body or b"")
        meter["early_stops"] += int(bool(getattr(resp, "stopped_early", False)))
    if resp.status == 404:
        return "not_found", 404, None, None
    if resp.status == 200:
        product, bare = detector.product, detector.bare_product
        if product is None and bare is None:
            product, bare = find_financial_products(resp.body.decode("utf-8", "replace"))
        if product is None:
            if bare is not None:   # a complete FinancialProduct without a usable description: final
                return "no_description", 200, "FinancialProduct without an acceptable description", None
            if getattr(resp, "truncated", False):
                return ("no_product_within_cap", 200,
                        f"no complete FinancialProduct in the first {len(resp.body)} bytes", None)
            return "no_description", 200, None, None
        note = validate(product, sid, url)
        return ("ok" if note is None else "mismatch"), 200, note, product
    if 300 <= resp.status < 400:
        return "redirect", resp.status, f"redirect: {resp.headers.get('Location') or resp.headers.get('location')}", None
    return "error", resp.status, f"http_{resp.status}", None


def crawl(cfg: Config, client: Any, *, limit: int | None = None, only_missing: bool = True,
          max_consecutive_errors: int = 3, progress_every: int = 100, flush_every: int = 50,
          on_blocked: Callable[[Blocked], None] | None = None, workers: int | None = DEFAULT_WORKERS,
          after_block: bool = False, countries: list[str] | None = None,
          min_mcap_usd: float | None = None) -> dict:
    """Crawl TradingView symbol pages for business descriptions, politely, resumably and WITHOUT holding the store.
    countries / min_mcap_usd narrow the queue (e.g. the China gap fill: countries=['CN'], min_mcap_usd=1e9).

    Connection rule (DuckDB allows one writer process and then refuses even read-only connections from other
    processes): the queue and the runs row are prepared in one short store.session; pages are fetched by `workers`
    threads (default 3, 1..4) that never touch the store, and no connection is held across a fetch; every `flush_every` pages (<= 500) and on stop / Blocked / interrupt / end a short store.session writes
    the batch (raw JSONL + snapshot + descriptions + crawl_state, one transaction) and the runs row, then closes.
    If the store is locked by another process at a mid-run flush, the batch is kept and retried only after another
    `flush_every` pages; after MAX_LOCKED_FLUSHES failed attempts in a row, or once MAX_BATCH pages are pending, the
    run stops ('store_locked') and makes a final, longer-waiting flush.

    Per page: 200 + valid FinancialProduct -> description + crawl_state 'ok'; complete 200 without one ->
    'no_description'; page cut at the 256 KB cap without one -> 'no_product_within_cap' (soft, retried later);
    404 -> 'not_found'; ticker/url validation failure -> 'mismatch'; 3xx -> 'redirect'; other statuses and
    network/HTTP-protocol errors -> 'error' (with note).
    http.Blocked (first 403/429/challenge) -> halt (set by http.Client the moment it sees the block: no further
    request starts) and the cooldown marker on disk, written by the worker thread that saw it (guard.mark_blocked,
    before the main thread handles the result or waits for the database); then crawl_state 'blocked', on_blocked,
    in-flight requests (<= workers - 1) finish and are recorded (at most DRAIN_MAX_S), flush, run status 'blocked'.
    Within COOLDOWN_HOURS of a recorded block (guard marker) the crawl refuses to start (CooldownActive, no request)
    unless after_block=True.
    `max_consecutive_errors` consecutive 'error' pages, in completion order, stop the run (soft statuses neither count
    nor reset). KeyboardInterrupt / SIGTERM (converted by the CLI): halt, drain in-flight, flush, 'interrupted'.
    Companies covered by SEC text (sec_covered) are dropped from the queue and, since the covered set is re-read at
    every flush, skipped mid-run before they are fetched.
    """
    with guard.budget_lock(cfg, guard.RATE_BUDGETS[guard.CMD_CRAWL_DESCRIPTIONS], reentrant=True):
        return _crawl(cfg, client, lambda wait_s: store.session(cfg, wait_s=wait_s), limit=limit,
                      only_missing=only_missing, max_consecutive_errors=max_consecutive_errors,
                      progress_every=progress_every, flush_every=flush_every, on_blocked=on_blocked,
                      workers=workers, after_block=after_block, countries=countries, min_mcap_usd=min_mcap_usd)


def crawl_with_connection(cfg: Config, con, client: Any, *, limit: int | None = None, only_missing: bool = True,
                          max_consecutive_errors: int = 3, progress_every: int = 100, batch_size: int = 50,
                          on_blocked: Callable[[Blocked], None] | None = None,
                          workers: int | None = DEFAULT_WORKERS, after_block: bool = False) -> dict:
    """Backward-compatible form of the old crawl(cfg, con, client, ...): same behaviour, but every write goes through
    the caller's connection, which therefore stays open for the whole run. Only for in-process callers that already
    own a connection (notebooks, tests); the CLI uses crawl()."""
    with guard.budget_lock(cfg, guard.RATE_BUDGETS[guard.CMD_CRAWL_DESCRIPTIONS], reentrant=True):
        return _crawl(cfg, client, lambda wait_s: contextlib.nullcontext(con), limit=limit,
                      only_missing=only_missing, max_consecutive_errors=max_consecutive_errors,
                      progress_every=progress_every, flush_every=batch_size, on_blocked=on_blocked,
                      workers=workers, after_block=after_block)


FLUSH_WAIT_S = 120.0   # mid-run flush: wait this long for another process's lock, then keep the batch and go on
FINAL_WAIT_S = 600.0   # final flush (end / stop / interrupt): wait longer, then raise StoreLocked
MAX_LOCKED_FLUSHES = 5  # consecutive locked mid-run flushes before the run stops ('store_locked')


@contextlib.contextmanager
def _sigterm_ignored() -> Iterator[None]:
    """Ignore SIGTERM while the final flush runs (a SIGTERM there would roll back the last batch); main thread only.

    The previous handler is restored afterwards. SIGKILL still works."""
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


def clamp_workers(workers: int | None) -> int:
    """Worker threads for the crawl: default DEFAULT_WORKERS (3), clamped to 1..MAX_WORKERS (4). The rate limit is
    enforced by the shared client limiter, not by the worker count."""
    if workers is None:
        return DEFAULT_WORKERS
    try:
        n = int(workers)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_WORKERS
    return max(1, min(n, MAX_WORKERS))


def _halt_event(client: Any) -> threading.Event:
    """The client's own halt Event (http.Client.halt: no new request starts once set), else a private one that the
    workers check before calling a client without it."""
    event = getattr(client, "halt", None)
    return event if isinstance(event, threading.Event) else threading.Event()


def _is_thread_safe(client: Any) -> bool:
    """Only a client that declares itself thread-safe (http.Client.thread_safe = True: locked limiter and counters)
    may be shared by several workers; anything else (older clients, wrappers, notebook fakes) gets one worker."""
    return getattr(client, "thread_safe", False) is True


class CooldownActive(RuntimeError):
    """crawl() refused to start: a block was recorded less than COOLDOWN_HOURS ago (pass after_block=True to
    override, as the CLI does for --after-block). No request was made."""


class _FirstBlock:
    """The run's first http.Blocked, recorded by the WORKER thread that saw it: the cooldown marker is written there
    at once (guard.mark_blocked never raises, atomic replace), before the main thread handles the result or waits for
    the database, so a SIGKILL/crash in between still leaves the 24 h cooldown on disk. Later blocks are ignored."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.lock = threading.Lock()
        self.error: Blocked | None = None
        self.url: str | None = None
        self.sid: str | None = None
        self.marker: Any = None

    def record(self, e: Blocked, url: str, sid: str) -> None:
        with self.lock:
            if self.error is not None:
                return
            self.error, self.url, self.sid = e, (getattr(e, "url", None) or url), sid
            self.marker = guard.mark_blocked(self.cfg, guard.CMD_CRAWL_DESCRIPTIONS, url=self.url,
                                             status=getattr(e, "status", None), reason=getattr(e, "reason", None))


def _fetch_one(client: Any, halt: threading.Event, block: _FirstBlock, sid: str, url: str
               ) -> tuple[str, Any, dict, float]:
    """Worker body: network fetch + parse only (no database, no shared counters).

    Returns (kind, value, meter, elapsed_s): ('page', (status, http_status, note, product)), ('blocked', Blocked),
    ('halted', None) when the request was never started, or ('raised', exception). On Blocked the halt Event is set
    (http.Client already set it when it saw the block) and the cooldown marker is written right here, before the
    main thread even sees the result."""
    meter = {"pages": 0, "bytes_read": 0, "early_stops": 0}
    t0 = time.monotonic()
    try:
        if halt.is_set():
            raise Halted(url)
        return "page", _run_page(client, sid, url, meter), meter, time.monotonic() - t0
    except Blocked as e:
        halt.set()
        block.record(e, url, sid)
        return "blocked", e, meter, time.monotonic() - t0
    except Halted:
        return "halted", None, meter, 0.0
    except BaseException as e:   # noqa: BLE001 - handed to the main thread, which re-raises it after draining
        return "raised", e, meter, time.monotonic() - t0


def _crawl(cfg: Config, client: Any, open_session: Callable[[float], Any], *, limit: int | None,
           only_missing: bool, max_consecutive_errors: int, progress_every: int, flush_every: int,
           on_blocked: Callable[[Blocked], None] | None = None, workers: int | None = DEFAULT_WORKERS,
           after_block: bool = False, countries: list[str] | None = None,
           min_mcap_usd: float | None = None) -> dict:
    if not after_block:
        hit = guard.recent_block_marker(cfg, guard.CMD_CRAWL_DESCRIPTIONS, COOLDOWN_HOURS)
        if hit is not None:   # checked BEFORE the halt flag is reset: no request within the cooldown
            raise CooldownActive(f"refusing to crawl within {COOLDOWN_HOURS:g} h of a block ({hit['note']}); "
                                 f"pass after_block=True to override")
    apply_rate(client, None)   # enforce the MAX_RATE ceiling; a slower client (--rate below 4.4) stays slower
    flush_every = max(1, min(int(flush_every), MAX_BATCH))
    workers = clamp_workers(workers)
    if workers > 1 and not _is_thread_safe(client):
        print(f"[{SOURCE_ID}] client {type(client).__name__} does not declare thread_safe; using 1 worker",
              file=sys.stderr, flush=True)
        workers = 1
    run_id = f"crawl-{SOURCE_ID}-{uuid.uuid4().hex[:12]}"
    with open_session(FINAL_WAIT_S) as con:
        covered = sec_covered(con)
        rows = _queue_rows(con, only_missing=only_missing, limit=limit, covered=covered, countries=countries,
                           min_mcap_usd=min_mcap_usd)
        attempts = dict(con.execute("SELECT security_id, attempts FROM crawl_state WHERE source_id = ?",
                                    [SOURCE_ID]).fetchall())
        con.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
                    [run_id, f"crawl {SOURCE_ID}", store.now_utc(), None, "running", 0, None])
    requests_before = int(getattr(client, "requests_made", 0) or 0)
    counts = {"ok": 0, "not_found": 0, "no_description": 0, "mismatch": 0, "redirect": 0,
              "no_product_within_cap": 0, "error": 0, "blocked": 0}
    summary: dict[str, Any] = {"run_id": run_id, "queued": len(rows), "attempted": 0, "snapshots": 0, "flushes": 0,
                               "stopped_reason": None, "blocked_at": None, "blocked_url": None,
                               "blocked_status": None, "blocked_reason": None, "skipped_sec_covered": 0,
                               "covered_refreshes": 0, "flush_error": None, "workers": workers,
                               "abandoned_in_flight": 0}
    meter = {"pages": 0, "bytes_read": 0, "early_stops": 0}

    def meter_text() -> str:
        mean_kb = meter["bytes_read"] / meter["pages"] / 1024 if meter["pages"] else 0.0
        return f"early_stops={meter['early_stops']} bytes_read={meter['bytes_read']} mean_kb={mean_kb:.1f}"
    batch = _Batch()
    consecutive_errors = 0
    locked_flushes = 0
    next_flush_at = flush_every     # pending pages at which the next mid-run flush is attempted
    completed = 0                   # pages whose result was recorded (progress lines)

    # Worker pool: workers only fetch + parse; this (main) thread owns the queue, the covered set, the batch, the
    # counters, the flushes, the progress lines and every stop decision. Every submitted page has its own Future,
    # created and registered BEFORE submit(), so a result can never be lost between two threads: it stays in the
    # Future until handle() has recorded it. Results are recorded in completion order.
    halt = _halt_event(client)
    halt.clear()                    # a new run (cooldown checked above); a block is remembered by the marker
    block = _FirstBlock(cfg)
    inflight: dict[int, tuple[str, str | None, str, dt.datetime, Future]] = {}
    handled: set[int] = set()       # tokens already recorded (a handle() interrupted before its pop is not redone)
    done_seq = itertools.count()    # completion order (next() on a count is atomic under the GIL)
    pending = iter(rows)
    tokens = itertools.count()
    raised: list[BaseException] = []
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tv-profile")

    def state(sid: str, status: str, http_status: int | None, note: str | None, at: dt.datetime) -> None:
        batch.states.append([SOURCE_ID, sid, status, http_status, (attempts.get(sid) or 0) + 1, at, note])
        counts[status] += 1

    def write(con, note: str | None, refresh: bool = False) -> None:
        nonlocal batch, covered
        if _flush(cfg, con, batch, attempts, note):
            summary["snapshots"] += 1
        if len(batch):
            summary["flushes"] += 1
        batch = _Batch()
        if refresh:   # sync-sec may be adding SEC text right now: re-read the covered set at every flush
            covered = sec_covered(con)
            summary["covered_refreshes"] += 1

    def flush_mid_run() -> None:
        """Short session; on another process's lock keep the batch and retry after another flush_every pages."""
        nonlocal locked_flushes, next_flush_at
        try:
            with open_session(FLUSH_WAIT_S) as con:
                write(con, None, refresh=True)
        except store.StoreLocked:
            locked_flushes += 1
            next_flush_at = len(batch) + flush_every
            print(f"[{SOURCE_ID}] store locked by another process; keeping {len(batch)} pending pages "
                  f"(locked flush {locked_flushes}/{MAX_LOCKED_FLUSHES})", file=sys.stderr, flush=True)
            if locked_flushes >= MAX_LOCKED_FLUSHES or len(batch) >= MAX_BATCH:
                summary["stopped_reason"] = "store_locked"
            return
        locked_flushes = 0
        next_flush_at = flush_every

    def stopping() -> bool:
        return bool(summary["stopped_reason"] or raised or halt.is_set())

    def fill() -> None:
        """Submit queue items until `workers` are in flight. The covered check runs just before each submit, so a
        company covered by SEC text after the crawl started (seen at the last flush) is never fetched."""
        while len(inflight) < workers and not stopping():
            item = next(pending, None)
            if item is None:
                return
            sid, company_key = item
            if _is_covered(covered, sid, company_key):   # became covered by SEC text after the crawl started
                summary["skipped_sec_covered"] += 1
                continue
            at = store.now_utc()
            summary["attempted"] += 1
            try:
                url = profile_url(sid)
            except ValueError as e:
                state(sid, "error", None, f"bad_security_id: {e}", at)
                continue
            fut: Future = Future()
            # register (with its Future) first, then submit: whatever an interrupt interrupts inside submit(), a
            # task that starts puts its result in `fut`; a task that never started is cancelled by the drain
            inflight[next(tokens)] = (sid, company_key, url, at, fut)
            pool.submit(task, fut, sid, url)

    def task(fut: Future, sid: str, url: str) -> None:
        if not fut.set_running_or_notify_cancel():   # cancelled before it started: nothing is sent
            return
        outcome = _fetch_one(client, halt, block, sid, url)   # never raises
        fut.set_result((next(done_seq), outcome))

    def drop_unstarted() -> None:
        """Cancel registered pages whose task has not started (Future.cancel() is atomic with the worker's
        set_running_or_notify_cancel, so a cancelled page is guaranteed never to be sent). Only called once halt
        is set; they stay in the queue for a later run."""
        for token, entry in list(inflight.items()):
            if entry[4].cancel():
                inflight.pop(token, None)
                summary["attempted"] -= 1

    def wait_results(timeout: float) -> list[tuple[int, tuple]]:
        """Finished (token, outcome) pairs in completion order; waits up to `timeout` for the first one
        (interruptible: SIGINT/SIGTERM land here). Nothing is removed from `inflight` here."""
        by_future = {entry[4]: token for token, entry in inflight.items()}
        if not by_future:
            return []
        done, _ = futures_wait(list(by_future), timeout=timeout, return_when=FIRST_COMPLETED)
        finished = []
        for fut in done:
            token = by_future[fut]
            if fut.cancelled():          # an interrupted drop_unstarted(): finish dropping it
                if inflight.pop(token, None) is not None:
                    summary["attempted"] -= 1
                continue
            seq, outcome = fut.result()
            finished.append((seq, token, outcome))
        finished.sort(key=lambda x: x[0])
        return [(token, outcome) for _, token, outcome in finished]

    def handle(token: int, outcome: tuple[str, Any, dict, float]) -> None:
        """Record one worker result (main thread only), in completion order. The token leaves `inflight` only
        after everything is recorded, so an interrupt in the middle leaves the Future to be handled again (the
        `handled` set, and the per-security de-duplication in _flush, keep that from recording it twice)."""
        entry = inflight.get(token)
        if entry is None:
            return
        if token not in handled:
            record(entry, outcome)
            handled.add(token)
        inflight.pop(token, None)

    def record(entry: tuple, outcome: tuple[str, Any, dict, float]) -> None:
        nonlocal consecutive_errors, completed
        sid, company_key, url, at, _ = entry
        kind, value, m, elapsed = outcome
        for k in meter:
            meter[k] += m[k]
        if kind == "halted":             # never started: nothing sent, left in the queue for a later run
            summary["attempted"] -= 1
            return
        batch.started_at = batch.started_at or at
        batch.requested.append(url)
        batch.duration_s += elapsed
        if kind == "raised":
            halt.set()
            raised.append(value)         # re-raised by the main loop once the other in-flight requests are in
            return
        if kind == "blocked":
            e = value
            state(sid, "blocked", e.status, f"blocked: {e.reason}", at)
            if summary["blocked_url"] is None:
                # first 403/429/challenge: the worker already set halt and wrote the marker (re-written here only
                # if that write failed); stop the whole run (no retry, no slow-down)
                first = block.error or e
                first_url = block.url or e.url or url
                if block.marker is None:
                    guard.mark_blocked(cfg, guard.CMD_CRAWL_DESCRIPTIONS, url=first_url, status=first.status,
                                       reason=first.reason)
                summary.update(stopped_reason="blocked", blocked_at=block.sid or sid, blocked_url=first_url, blocked_status=first.status, blocked_reason=first.reason)
                print(f"[{SOURCE_ID}] blocked at {first_url} (status={first.status}, reason={first.reason}); "
                      f"stopping the run, draining {len(inflight) - 1} in-flight request(s), flushing progress",
                      file=sys.stderr, flush=True)
                if on_blocked is not None:
                    with contextlib.suppress(Exception):
                        on_blocked(first)
            return
        status, http_status, note, product = value
        if product is not None:
            batch.lines.append({"security_id": sid, "url": url, "http_status": http_status,
                                "fetched_at": at.isoformat(), "status": status, "note": note, "product": product})
            if status == "ok":
                batch.descriptions.append({"security_id": sid, "company_key": company_key, "url": url,
                                           "text": product["description"], "fetched_at": at})
        state(sid, status, http_status, note, at)
        completed += 1
        if status == "error":            # counted in completion order
            consecutive_errors += 1
        elif status not in SOFT_STATUSES:
            consecutive_errors = 0
        if consecutive_errors >= max_consecutive_errors and not summary["stopped_reason"]:
            summary["stopped_reason"] = "consecutive_errors"
            halt.set()
        if progress_every and completed % progress_every == 0:
            print(f"[{SOURCE_ID}] {completed}/{len(rows)} {counts} {meter_text()}", file=sys.stderr, flush=True)

    try:
        fill()
        while inflight:
            for token, outcome in wait_results(RESULT_POLL_S):
                handle(token, outcome)
            if stopping():
                halt.set()               # no new start anywhere; the loop only drains what is in flight
                continue
            if len(batch) >= next_flush_at:
                flush_mid_run()
                if summary["stopped_reason"]:
                    halt.set()
                    continue
            fill()
        if raised:
            raise raised[0]
    except (KeyboardInterrupt, SystemExit):
        halt.set()
        if summary["stopped_reason"] != "blocked":
            summary["stopped_reason"] = "interrupted"
        raise
    except BaseException:
        halt.set()
        if summary["stopped_reason"] != "blocked":
            summary["stopped_reason"] = "error"
        raise
    finally:
        # drain: requests already on the wire finish and are recorded normally (at most workers - 1 after a block),
        # for at most DRAIN_MAX_S; pages that never started are cancelled at once (nothing sent)
        try:
            if inflight:
                halt.set()
                drop_unstarted()
            deadline = time.monotonic() + DRAIN_MAX_S
            while inflight:
                left = deadline - time.monotonic()
                if left <= 0:
                    print(f"[{SOURCE_ID}] drain limit {DRAIN_MAX_S:g} s reached; {len(inflight)} in-flight "
                          f"request(s) left unrecorded (retried next run)", file=sys.stderr, flush=True)
                    break
                for token, outcome in wait_results(min(RESULT_POLL_S, left)):
                    handle(token, outcome)
                drop_unstarted()
        except (KeyboardInterrupt, SystemExit):   # a second Ctrl-C while draining: stop waiting (already raising)
            halt.set()
        summary["abandoned_in_flight"] = len(inflight)
        if block.error is not None and summary["blocked_url"] is None:
            # an abandoned in-flight request was blocked: its worker already wrote the marker; report it too
            summary.update(stopped_reason="blocked", blocked_at=block.sid, blocked_url=block.url,
                           blocked_status=block.error.status, blocked_reason=block.error.reason)
            if on_blocked is not None:
                with contextlib.suppress(Exception):
                    on_blocked(block.error)
        # abandoned threads are not waited for here; the interpreter still joins them at exit (each socket
        # operation is bounded by the client's timeout_s)
        pool.shutdown(wait=not inflight, cancel_futures=True)
        requests = int(getattr(client, "requests_made", 0) or 0) - requests_before
        run_status = {"blocked": "blocked", "consecutive_errors": "stopped_errors", "interrupted": "interrupted",
                      "store_locked": "store_locked", "error": "error"}.get(summary["stopped_reason"], "ok")
        summary.update(counts, requests=requests, status=run_status,
                       remaining=len(rows) - summary["attempted"] - summary["skipped_sec_covered"],
                       pages_read=meter["pages"], early_stops=meter["early_stops"], bytes_read=meter["bytes_read"],
                       mean_kb_per_page=round(meter["bytes_read"] / meter["pages"] / 1024, 1)
                       if meter["pages"] else None)
        try:
            with _sigterm_ignored(), open_session(FINAL_WAIT_S) as con:
                write(con, f"run stopped: {summary['stopped_reason']}" if summary["stopped_reason"] else None)
                note = {**counts, "early_stops": meter["early_stops"], "bytes_read": meter["bytes_read"],
                        "skipped_sec_covered": summary["skipped_sec_covered"], "workers": workers}
                if summary["blocked_url"]:
                    note.update(blocked_url=summary["blocked_url"], blocked_status=summary["blocked_status"],
                                blocked_reason=summary["blocked_reason"])
                con.execute("UPDATE runs SET finished_at = ?, status = ?, requests = ?, note = ? WHERE run_id = ?",
                            [store.now_utc(), run_status, requests, json.dumps(note), run_id])
        except BaseException as e:
            if summary["stopped_reason"] != "blocked":
                raise
            # blocked: the cooldown marker is already on disk; never let a failed final write (StoreLocked, disk,
            # Ctrl-C) hide the block from the caller. The pending batch is lost; its pages are retried next run.
            summary["flush_error"] = f"{type(e).__name__}: {str(e)[:300]}"
            print(f"[{SOURCE_ID}] final write after the block failed: {summary['flush_error']}", file=sys.stderr,
                  flush=True)
    return summary
