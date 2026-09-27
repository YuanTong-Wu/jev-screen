"""DuckDB store: schema, snapshot journal and small upsert helpers.

Rules (see docs/DATA_RULES.md):
- Raw provider payloads are written verbatim under data/raw/<source>/<YYYY-MM-DD>/ with sha256 in `snapshots`.
- Every data row carries snapshot_id; licence tier is derived via snapshots.source_id -> provenance.SOURCES.
- Missing stays missing (NULL), never zero.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Sequence

import duckdb

from . import provenance
from .config import Config

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS sources (
    source_id VARCHAR PRIMARY KEY, name VARCHAR, license_tier VARCHAR, terms_url VARCHAR, notes VARCHAR);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id VARCHAR PRIMARY KEY, source_id VARCHAR, kind VARCHAR, fetched_at TIMESTAMP,
    request_json VARCHAR, raw_path VARCHAR, raw_sha256 VARCHAR, raw_bytes BIGINT,
    rows BIGINT, duration_s DOUBLE, status VARCHAR, note VARCHAR);

-- One row per TradingView listing line. company_key groups lines of the same company.
CREATE TABLE IF NOT EXISTS securities (
    security_id VARCHAR PRIMARY KEY,          -- 'EXCHANGE:SYMBOL' exactly as TradingView returns in field s
    exchange VARCHAR, symbol VARCHAR, name VARCHAR, isin VARCHAR, country VARCHAR,
    tv_type VARCHAR, tv_subtype VARCHAR, is_primary BOOLEAN,
    price_currency VARCHAR,                    -- e.g. GBX, ILA, ZAC, JPY
    fundamental_currency VARCHAR,              -- local-mode fundamental currency, main unit (GBP for GBX)
    sector VARCHAR, industry VARCHAR,
    company_key VARCHAR,                       -- 'isin:<ISIN>' if ISIN present else 'sec:<security_id>'
    first_seen_snapshot VARCHAR, last_seen_snapshot VARCHAR, last_seen_at TIMESTAMP, active BOOLEAN);

CREATE TABLE IF NOT EXISTS market_daily (
    security_id VARCHAR, as_of DATE,
    close DOUBLE, price_currency VARCHAR, volume DOUBLE, avg_volume_10d DOUBLE,
    market_cap_usd DOUBLE, market_cap_local DOUBLE,
    fx_local_to_usd DOUBLE,                    -- market_cap_usd / market_cap_local (current rate); NULL if either missing
    shares_outstanding DOUBLE, float_shares DOUBLE,
    snapshot_id VARCHAR, PRIMARY KEY (security_id, as_of));

-- Latest TTM / latest-FY values. value_usd as returned by TradingView default mode (period-end FX for FY!),
-- value_local from to_symbol:false mode. Never mix value_usd with fundamentals_annual.
CREATE TABLE IF NOT EXISTS fundamentals_current (
    security_id VARCHAR, as_of DATE, metric VARCHAR, period VARCHAR,   -- period: 'ttm' | 'fy'
    value_usd DOUBLE, value_local DOUBLE, currency_local VARCHAR, fiscal_period_end DATE,
    snapshot_id VARCHAR, PRIMARY KEY (security_id, as_of, metric, period));

-- Annual history from *_fy_h columns, always in fundamental_currency (local main unit). Latest pull overwrites.
CREATE TABLE IF NOT EXISTS fundamentals_annual (
    security_id VARCHAR, fiscal_year INTEGER, metric VARCHAR,
    value_local DOUBLE, currency_local VARCHAR, snapshot_id VARCHAR,
    PRIMARY KEY (security_id, fiscal_year, metric));

CREATE TABLE IF NOT EXISTS descriptions (
    security_id VARCHAR, source_id VARCHAR, company_key VARCHAR,
    text VARCHAR, text_sha256 VARCHAR, lang VARCHAR, source_url VARCHAR, fetched_at TIMESTAMP,
    match_method VARCHAR, match_score DOUBLE, snapshot_id VARCHAR,
    PRIMARY KEY (security_id, source_id));

-- Official long-form evidence (e.g. 10-K Item 1). Text lives in a file under data/docs/; the table keeps metadata.
CREATE TABLE IF NOT EXISTS documents (
    doc_id VARCHAR PRIMARY KEY,               -- '<source_id>:<cik>:<accession>:<section>'
    security_id VARCHAR, company_key VARCHAR, source_id VARCHAR,
    cik VARCHAR, form VARCHAR, section VARCHAR,  -- section: 'item1' | 'item4'
    accession VARCHAR, filing_date DATE, report_date DATE, url VARCHAR,
    raw_sha256 VARCHAR, raw_bytes BIGINT,      -- of the full primary document (not stored)
    text_path VARCHAR, text_sha256 VARCHAR, text_chars BIGINT,
    extractor VARCHAR, extract_note VARCHAR, fetched_at TIMESTAMP, snapshot_id VARCHAR);

-- Identifier crosswalk (e.g. TradingView line -> SEC CIK), one row per (security_id, id_type).
CREATE TABLE IF NOT EXISTS identifiers (
    security_id VARCHAR, id_type VARCHAR, id_value VARCHAR, method VARCHAR, snapshot_id VARCHAR,
    PRIMARY KEY (security_id, id_type));

-- Paid Jev calls. One row per physical request (request_id = payload sha256[:24] + '-' + a per-send suffix, so a
-- resend of the same payload is a new row and no charge is overwritten); cost is accounted once per request, never
-- per item. status: sent | ok | failed | uncertain (sent but outcome unknown: never resend blindly).
CREATE TABLE IF NOT EXISTS jev_requests (
    request_id VARCHAR PRIMARY KEY, run_id VARCHAR, layer VARCHAR, payload_sha256 VARCHAR, model VARCHAR,
    items INTEGER, questions INTEGER, status VARCHAR, http_status INTEGER,
    cost_usd DOUBLE, cost_basis VARCHAR, input_tokens BIGINT, output_tokens BIGINT,
    sent_at TIMESTAMP, completed_at TIMESTAMP, response_path VARCHAR, error VARCHAR);

-- Per-item state of each Jev send and the answer reuse cache. item_key = sha256(model, question, issuer, text)[:32]:
-- independent of packing, so an item answered once is reused whatever packet it would land in next time.
-- status: sent | ok | failed | uncertain; label/probs_json only for 'ok'. read_index and confidence are added by
-- MIGRATIONS below (item_key is salted with the read index only when it is > 0).
CREATE TABLE IF NOT EXISTS jev_items (
    item_key VARCHAR, request_id VARCHAR, run_id VARCHAR, layer VARCHAR, position INTEGER, status VARCHAR,
    label VARCHAR, probs_json VARCHAR, error VARCHAR, created_at TIMESTAMP, PRIMARY KEY (item_key, request_id));

CREATE TABLE IF NOT EXISTS screen_runs (
    run_id VARCHAR PRIMARY KEY, idea VARCHAR, params_json VARCHAR, started_at TIMESTAMP, finished_at TIMESTAMP,
    status VARCHAR, universe_n INTEGER, l1_n INTEGER, l1_pass_n INTEGER, l2_n INTEGER, output_n INTEGER,
    cost_usd DOUBLE, output_dir VARCHAR, note VARCHAR);

-- Per-company judgements of one screen run. input_tier = licence tier of the text Jev read
-- (gray-private for TradingView/FinanceDatabase descriptions, official-private for SEC excerpts).
-- reads_json, p_pos and p_pos_sd (repeated L2 reads) are added by MIGRATIONS below.
CREATE TABLE IF NOT EXISTS screen_results (
    run_id VARCHAR, company_key VARCHAR, security_id VARCHAR, layer VARCHAR,   -- 'l1' | 'l2'
    label VARCHAR, probs_json VARCHAR, p_top DOUBLE, request_id VARCHAR,
    input_source VARCHAR, input_tier VARCHAR, evidence_url VARCHAR, evidence_excerpt VARCHAR,
    status VARCHAR, error VARCHAR,
    PRIMARY KEY (run_id, company_key, layer));

CREATE TABLE IF NOT EXISTS crawl_state (
    source_id VARCHAR, security_id VARCHAR, status VARCHAR,   -- ok | not_found | no_description | mismatch | redirect | no_product_within_cap | blocked | error
    http_status INTEGER, attempts INTEGER, last_attempt_at TIMESTAMP, note VARCHAR,
    PRIMARY KEY (source_id, security_id));

CREATE TABLE IF NOT EXISTS runs (
    run_id VARCHAR PRIMARY KEY, command VARCHAR, started_at TIMESTAMP, finished_at TIMESTAMP,
    status VARCHAR, requests INTEGER, note VARCHAR);

-- Translations of page texts made by the user's own AI agent (jevscreen.translations), keyed by the sha256 of the
-- original text and the target language; provenance 'agent translation'. The original (the evidence) is never
-- stored here nor changed: a page shows the translation with an 'AI translation' tag and the original beside it.
CREATE TABLE IF NOT EXISTS translations (
    sha VARCHAR, target_lang VARCHAR, kind VARCHAR, source_lang VARCHAR, text VARCHAR, provenance VARCHAR,
    created_at TIMESTAMP, PRIMARY KEY (sha, target_lang));

-- Screenable universe: active primary common stocks plus primary depositary receipts, one row per company_key
-- (highest USD market cap line wins), joined to the latest market row.
CREATE OR REPLACE VIEW latest_market AS
    SELECT m.* FROM market_daily m
    JOIN (SELECT security_id, max(as_of) AS as_of FROM market_daily GROUP BY 1) x USING (security_id, as_of);

CREATE OR REPLACE VIEW universe AS
    WITH lines AS (
        SELECT s.*, lm.market_cap_usd, lm.close, lm.as_of AS market_as_of,
               row_number() OVER (PARTITION BY s.company_key ORDER BY lm.market_cap_usd DESC NULLS LAST, s.security_id) AS rn
        FROM securities s LEFT JOIN latest_market lm USING (security_id)
        WHERE s.active AND s.is_primary AND (
              (s.tv_type = 'stock' AND s.tv_subtype = 'common') OR s.tv_type = 'dr'))
    SELECT * EXCLUDE (rn) FROM lines WHERE rn = 1;
"""


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def connect(cfg: Config, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    cfg.ensure()
    con = duckdb.connect(str(cfg.db_path), read_only=read_only)
    if not read_only:
        init(con)
    return con


class StoreLocked(RuntimeError):
    """Another process held the DuckDB write lock for longer than the caller was willing to wait."""


@contextlib.contextmanager
def session(cfg: Config, *, read_only: bool = False, wait_s: float | None = None, poll_s: float = 0.5):
    """Short-lived connection. Long jobs must NOT hold a connection while doing network I/O:
    open a session per batch, write, close. DuckDB allows one writer process and blocks readers too,
    so both readers and writers retry on the lock until `wait_s` elapses, then raise StoreLocked.
    wait_s None: env JEVSCREEN_STORE_WAIT_S (set by the on-demand fetch for its child processes), else 600 s.
    """
    cfg.ensure()
    if wait_s is None:
        wait_s = float(os.environ.get("JEVSCREEN_STORE_WAIT_S") or 600.0)
    deadline = time.monotonic() + wait_s
    while True:
        try:
            con = duckdb.connect(str(cfg.db_path), read_only=read_only)
            break
        except duckdb.IOException as e:
            if "lock" not in str(e).lower() or time.monotonic() >= deadline:
                if "lock" in str(e).lower():
                    raise StoreLocked(str(e)) from e
                raise
            time.sleep(poll_s)
    try:
        if not read_only:
            init(con)
        yield con
    finally:
        con.close()


# Columns added after the first schema. Run after SCHEMA on every init; ADD COLUMN IF NOT EXISTS makes them idempotent,
# and an old database gains them (NULL for existing rows) the first time a writer opens it. New tables get them the
# same way, so old and new databases end up with the same column order.
# jev_items: read_index = Question.read of the send (repeat reads of one item), confidence = Jev's per-answer value.
# jev_requests: provider = the Jev provider of the send (typesafe | openrouter | vercel; NULL before: OpenRouter).
# screen_results: reads_json = [{read, request_id, p_pos, cached}], p_pos / p_pos_sd = mean / spread over the reads.
MIGRATIONS = (
    "ALTER TABLE jev_items ADD COLUMN IF NOT EXISTS read_index INTEGER",
    "ALTER TABLE jev_items ADD COLUMN IF NOT EXISTS confidence DOUBLE",
    "ALTER TABLE jev_requests ADD COLUMN IF NOT EXISTS provider VARCHAR",
    "ALTER TABLE screen_results ADD COLUMN IF NOT EXISTS reads_json VARCHAR",
    "ALTER TABLE screen_results ADD COLUMN IF NOT EXISTS p_pos DOUBLE",
    "ALTER TABLE screen_results ADD COLUMN IF NOT EXISTS p_pos_sd DOUBLE",
)


def init(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(SCHEMA)
    for stmt in MIGRATIONS:
        con.execute(stmt)
    for s in provenance.SOURCES.values():
        con.execute("INSERT OR REPLACE INTO sources VALUES (?, ?, ?, ?, ?)",
                    [s.source_id, s.name, s.tier.value, s.terms_url, s.notes])


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_raw(cfg: Config, source_id: str, kind: str, raw: bytes, suffix: str = "json") -> tuple[str, str]:
    """Write a raw payload verbatim; returns (path, sha256). File name embeds the hash so writes are idempotent."""
    digest = sha256(raw)
    day = now_utc().strftime("%Y-%m-%d")
    folder = cfg.raw_dir / source_id / day
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{kind}-{digest[:16]}.{suffix}"
    if not path.exists():
        path.write_bytes(raw)
    return str(path), digest


def record_snapshot(con, *, source_id: str, kind: str, request: Any, raw_path: str | None, raw_sha256: str | None,
                    raw_bytes: int | None, rows: int | None, duration_s: float | None, status: str = "ok",
                    note: str | None = None, fetched_at: dt.datetime | None = None) -> str:
    snapshot_id = f"{source_id}:{kind}:{uuid.uuid4().hex[:12]}"
    con.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
        snapshot_id, source_id, kind, fetched_at or now_utc(),
        json.dumps(request, ensure_ascii=False, sort_keys=True) if request is not None else None,
        raw_path, raw_sha256, raw_bytes, rows, duration_s, status, note])
    return snapshot_id


def upsert_many(con, table: str, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> int:
    """INSERT OR REPLACE rows (tables above all have primary keys). Returns row count."""
    rows = list(rows)
    if not rows:
        return 0
    placeholders = ",".join("?" for _ in columns)
    con.executemany(f"INSERT OR REPLACE INTO {table} ({','.join(columns)}) VALUES ({placeholders})", rows)
    return len(rows)


def company_key(isin: str | None, security_id: str) -> str:
    isin = (isin or "").strip().upper()
    return f"isin:{isin}" if len(isin) == 12 else f"sec:{security_id}"


def license_tier_for_snapshot(con, snapshot_id: str) -> str | None:
    row = con.execute("SELECT s.license_tier FROM snapshots n JOIN sources s USING (source_id) WHERE n.snapshot_id = ?",
                      [snapshot_id]).fetchone()
    return row[0] if row else None
