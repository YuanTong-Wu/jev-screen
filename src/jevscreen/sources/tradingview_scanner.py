"""TradingView screener adapter (source 'tradingview_scanner', licence tier gray-private).

Data rules implemented here (docs/DATA_RULES.md):
- Universe = one POST per filter: primary common stocks plus primary depositary receipts (the DR set keeps BABA, PDD,
  ARM, EURONEXT:ABN ... in the universe).
- Every filter is pulled twice: default mode (USD market cap / TTM / FY values) and local mode
  (`price_conversion.to_symbol = false`) which yields local values and the real fundamental currency code.
  USD values come only from the default pull, local values only from the local pull; they are never mixed.
- `fx_local_to_usd = market_cap_usd / market_cap_local` from the same day.
- `*_fy_h` history arrays are local currency; stored with `securities.fundamental_currency`.
- Unknown columns silently return null, so column coverage is computed and reported after every pull.
- Every raw payload is saved verbatim and journaled as a snapshot; missing values stay NULL, never 0.
- HTTP 403/429/challenge (http.Blocked) aborts the run without deactivating anything.
- Deactivation needs a COMPLETE pull: it is skipped (run status 'partial', with a warning) when any universe request
  returns 0 rows, reports totalCount > rows returned, is truncated, or returns < 90% of the rows of the previous ok
  snapshot of the same kind.
- fundamentals_current gets a row for every metric of every security seen, NULL when TradingView has no value, so
  the latest row never silently repeats an older value.
- Each universe snapshot's note names its paired other-mode snapshot ('paired_snapshot'): market/fundamental rows
  point to the USD snapshot, and their local values are traceable through that pairing.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import math
import os
import tempfile
import time
import uuid
from typing import Any, Iterable, Mapping, Sequence

from .. import store
from ..config import Config
from ..http import Blocked

SOURCE_ID = "tradingview_scanner"
SCAN_URL = "https://scanner.tradingview.com/global/scan"
SCAN_RANGE = 100_000  # the full universe is ~50k rows; a truncated result is reported as a warning
MIN_ROW_RATIO = 0.9   # below this share of the previous ok snapshot's rows, a pull counts as partial

CURRENT_COLUMNS: list[str] = [
    "name", "description", "type", "subtype", "is_primary", "exchange", "country", "isin", "currency",
    "fundamental_currency_code", "close", "volume", "average_volume_10d_calc", "market_cap_basic",
    "total_shares_outstanding", "float_shares_outstanding", "sector", "industry", "number_of_employees",
    "total_revenue_ttm", "net_income_ttm", "gross_profit_ttm", "free_cash_flow_ttm", "total_revenue_fy",
    "net_income_fy", "fiscal_period_end_fy",
]

HISTORY_METRICS: list[str] = [
    "total_revenue", "net_income", "gross_profit", "ebitda", "free_cash_flow", "total_assets", "total_debt",
    "earnings_per_share_diluted",
]
HISTORY_PERIOD_COLUMN = "fiscal_period_fy_h"
HISTORY_COLUMNS: list[str] = [f"{m}_fy_h" for m in HISTORY_METRICS]


def _eq(left: str, right: Any) -> dict[str, Any]:
    return {"left": left, "operation": "equal", "right": right}


UNIVERSE_FILTERS: dict[str, list[dict[str, Any]]] = {
    "stock": [_eq("type", "stock"), _eq("is_primary", True), _eq("subtype", "common")],
    "dr": [_eq("type", "dr"), _eq("is_primary", True)],
}

# (metric, period, source column) for fundamentals_current.
CURRENT_FUNDAMENTALS: list[tuple[str, str, str]] = [
    ("total_revenue", "ttm", "total_revenue_ttm"),
    ("net_income", "ttm", "net_income_ttm"),
    ("gross_profit", "ttm", "gross_profit_ttm"),
    ("free_cash_flow", "ttm", "free_cash_flow_ttm"),
    ("total_revenue", "fy", "total_revenue_fy"),
    ("net_income", "fy", "net_income_fy"),
]

Row = dict[str, Any]


# ----------------------------------------------------------------------------------------------- pure helpers

def _num(x: Any) -> float | None:
    """Finite number or None (bools and strings are not numbers; missing never becomes 0)."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    x = float(x)
    return x if math.isfinite(x) else None


def _txt(x: Any) -> str | None:
    """Non-empty string or None (TradingView returns '' for e.g. the subtype of DRs)."""
    if x is None:
        return None
    s = str(x).strip()
    return s or None


def _year(x: Any) -> int | None:
    if isinstance(x, str) and x.strip().isdigit():
        return int(x.strip())
    n = _num(x)
    return int(n) if n is not None and n == int(n) else None


def build_scan_body(filters: Sequence[Mapping[str, Any]] | None, columns: Sequence[str], local: bool,
                    range_: tuple[int, int] | list[int] | None = None,
                    tickers: Sequence[str] | None = None) -> dict[str, Any]:
    """Scanner request body.

    Rule: local=True adds `price_conversion.to_symbol=false` (local currency values and the real fundamental currency).
    Explicit tickers replace the filter; filter queries get a range and a stable sort by name.
    """
    body: dict[str, Any] = {"columns": list(columns), "options": {"lang": "en"}}
    if tickers is not None:
        body["symbols"] = {"tickers": list(tickers)}
    else:
        body["filter"] = [dict(f) for f in (filters or [])]
        body["range"] = list(range_) if range_ is not None else [0, SCAN_RANGE]
        body["sort"] = {"sortBy": "name", "sortOrder": "asc"}
    if local:
        body["price_conversion"] = {"to_symbol": False}
    return body


def parse_rows(payload: Mapping[str, Any], columns: Sequence[str]) -> list[Row]:
    """Map `{"data": [{"s": ..., "d": [...]}]}` to dicts keyed by 's' and column names.

    Rule: every `d` must have exactly one value per requested column, otherwise the payload is rejected (ValueError).
    """
    if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
        raise ValueError("scanner payload has no 'data' list")
    out: list[Row] = []
    for i, item in enumerate(payload["data"]):
        if not isinstance(item, Mapping) or not isinstance(item.get("s"), str) or not isinstance(item.get("d"), list):
            raise ValueError(f"scanner row {i} is not {{s: str, d: list}}")
        d = item["d"]
        if len(d) != len(columns):
            raise ValueError(f"scanner row {i} ({item['s']}) has {len(d)} values for {len(columns)} columns")
        row: Row = {"s": item["s"]}
        row.update(zip(columns, d))
        out.append(row)
    return out


def column_coverage(rows: Iterable[Mapping[str, Any]], columns: Sequence[str]) -> dict[str, int]:
    """Non-null count per column. Rule: unknown columns silently return null, so zero coverage flags a bad name."""
    cov = {c: 0 for c in columns}
    for r in rows:
        for c in columns:
            v = r.get(c)
            if v is not None and v != "" and v != []:
                cov[c] += 1
    return cov


def fx_local_to_usd(usd_row: Mapping[str, Any] | None, local_row: Mapping[str, Any] | None) -> float | None:
    """Rule: fx_local_to_usd = market_cap_usd / market_cap_local (same day), only when both are > 0."""
    usd = _num((usd_row or {}).get("market_cap_basic"))
    loc = _num((local_row or {}).get("market_cap_basic"))
    if usd is None or loc is None or usd <= 0 or loc <= 0:
        return None
    return usd / loc


def epoch_to_date(x: Any) -> dt.date | None:
    """fiscal_period_end_fy is epoch seconds (UTC); anything unparsable stays None."""
    n = _num(x)
    if n is None:
        return None
    try:
        return dt.datetime.fromtimestamp(n, tz=dt.timezone.utc).date()
    except (OverflowError, OSError, ValueError):
        return None


def zip_history(periods: Any, values: Any) -> list[tuple[int, float | None]]:
    """Zip `fiscal_period_fy_h` with a `*_fy_h` array by index (index 0 = latest).

    Rules: only the overlapping prefix is zipped when lengths differ (caller records a note); entries whose period is
    null are skipped; null values are kept as None. A missing array yields no entries.
    """
    if not isinstance(periods, list) or not isinstance(values, list):
        return []
    out: list[tuple[int, float | None]] = []
    for p, v in zip(periods, values):
        fy = _year(p)
        if fy is None:
            continue
        out.append((fy, _num(v)))
    return out


def _split_symbol(security_id: str) -> tuple[str | None, str]:
    if ":" in security_id:
        exchange, symbol = security_id.split(":", 1)
        return exchange or None, symbol
    return None, security_id


BULK_MIN_ROWS = 2000     # from this many rows _upsert stages them through a temporary NDJSON file (read_json)


def _upsert(con: Any, table: str, columns: Sequence[str], rows: Sequence[Sequence[Any]], chunk: int = 1000) -> int:
    """INSERT OR REPLACE of many rows (store.upsert_many's executemany is too slow for ~10^6 history rows).

    Large batches go through one temporary NDJSON file read by DuckDB's read_json with the target's column types:
    about 15x faster than multi-row VALUES statements (measured on a replay of a full universe pull: the ~300k
    fundamentals rows took ~12 s as VALUES chunks on an idle machine, several minutes on a loaded one; < 1 s this
    way). The file lives only for the statement (mode 0600, deleted after). If read_json is unavailable the
    multi-row VALUES path runs instead. Callers guarantee primary keys are unique within `rows`.
    """
    if len(rows) >= BULK_MIN_ROWS:
        with contextlib.suppress(_BulkUnavailable):
            return _upsert_bulk(con, table, columns, rows)
    one = "(" + ",".join("?" for _ in columns) + ")"
    for i in range(0, len(rows), chunk):
        part = rows[i:i + chunk]
        con.execute(f"INSERT OR REPLACE INTO {table} ({','.join(columns)}) VALUES {','.join([one] * len(part))}",
                    [v for r in part for v in r])
    return len(rows)


class _BulkUnavailable(RuntimeError):
    """The NDJSON path could not run before anything was written (the caller falls back to VALUES)."""


def _json_value(v: Any) -> Any:
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat(sep=" ") if isinstance(v, dt.datetime) else v.isoformat()
    return v


def _upsert_bulk(con: Any, table: str, columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> int:
    types = dict(con.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = ?",
                             [table]).fetchall())
    if any(c not in types for c in columns):
        raise _BulkUnavailable(f"{table}: unknown columns")
    fd, path = tempfile.mkstemp(prefix="jevscreen-upsert-", suffix=".ndjson")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps({c: _json_value(v) for c, v in zip(columns, r)}, ensure_ascii=False,
                                   allow_nan=False))
                f.write("\n")
        spec = "{" + ", ".join(f"'{c}': '{types[c]}'" for c in columns) + "}"
        cols = ",".join(columns)
        try:
            con.execute(f"INSERT OR REPLACE INTO {table} ({cols}) SELECT {cols} FROM read_json(?, "
                        f"format = 'newline_delimited', columns = {spec})", [path])
        except Exception as e:  # noqa: BLE001 - e.g. no json extension: nothing was written by the failed statement
            if "json" in str(e).lower() and ("extension" in str(e).lower() or "read_json" in str(e).lower()):
                raise _BulkUnavailable(str(e)[:200]) from None
            raise
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)
    return len(rows)


# ----------------------------------------------------------------------------------------------- fetching

class _Fetcher:
    """Posts scanner requests, saves raw payloads and journals one snapshot per request."""

    def __init__(self, cfg: Config, con: Any, client: Any, warnings: list[str]):
        self.cfg, self.con, self.client, self.warnings = cfg, con, client, warnings
        self.requests = 0
        self.partial: str | None = None

    def _snapshot(self, kind: str, body: Any, t0: float, *, raw_path: str | None = None, digest: str | None = None,
                  raw_bytes: int | None = None, rows: int | None = None, status: str = "ok",
                  note: str | None = None) -> str:
        return store.record_snapshot(self.con, source_id=SOURCE_ID, kind=kind, request=body, raw_path=raw_path,
                                     raw_sha256=digest, raw_bytes=raw_bytes, rows=rows,
                                     duration_s=round(time.monotonic() - t0, 3), status=status, note=note)

    def fetch(self, kind: str, body: dict[str, Any], columns: Sequence[str]) -> tuple[list[Row], str, dict[str, int]]:
        t0 = time.monotonic()
        self.requests += 1
        try:
            resp = self.client.post_json(SCAN_URL, body)
        except Blocked as e:
            self._snapshot(kind, body, t0, status="blocked", note=str(e))
            raise
        raw: bytes = resp.body
        path, digest = store.save_raw(self.cfg, SOURCE_ID, kind, raw)
        meta = dict(raw_path=path, digest=digest, raw_bytes=len(raw))
        if resp.status != 200:
            self._snapshot(kind, body, t0, status="error", note=f"http_{resp.status}", **meta)
            raise RuntimeError(f"tradingview scanner returned HTTP {resp.status} for {kind}")
        if getattr(resp, "truncated", False):
            self._snapshot(kind, body, t0, status="error", note="truncated at read cap", **meta)
            raise RuntimeError(f"tradingview scanner response for {kind} hit the read cap (truncated)")
        try:
            payload = json.loads(raw)
            rows = parse_rows(payload, columns)
        except ValueError as e:  # json.JSONDecodeError is a ValueError
            self._snapshot(kind, body, t0, status="error", note=f"parse: {e}", **meta)
            raise
        cov = column_coverage(rows, columns)
        total = payload.get("totalCount")
        self.partial = None
        if isinstance(total, int) and total > len(rows):
            self.warnings.append(f"{kind}: totalCount {total} > {len(rows)} rows returned (truncated)")
            self.partial = f"{kind}: totalCount {total} > {len(rows)} rows"
        note = json.dumps({"coverage": cov, "totalCount": total}, sort_keys=True)
        snap = self._snapshot(kind, body, t0, rows=len(rows), note=note, **meta)
        return rows, snap, cov

    def completeness_problem(self, kind: str, snap: str, n_rows: int) -> str | None:
        """Rule: a universe pull is complete only with rows > 0, totalCount <= rows and >= MIN_ROW_RATIO of the
        previous ok snapshot of the same kind. Returns the problem, or None when complete."""
        if self.partial:
            return self.partial
        if n_rows == 0:
            return f"{kind}: 0 rows"
        prev = self.con.execute(
            "SELECT rows FROM snapshots WHERE source_id = ? AND kind = ? AND status = 'ok' AND snapshot_id <> ? "
            "AND rows IS NOT NULL ORDER BY fetched_at DESC LIMIT 1", [SOURCE_ID, kind, snap]).fetchone()
        if prev and n_rows < MIN_ROW_RATIO * prev[0]:
            return f"{kind}: {n_rows} rows < {MIN_ROW_RATIO:.0%} of previous {prev[0]}"
        return None

    def pair(self, a: str, b: str) -> None:
        """Record each snapshot's other-mode partner in its JSON note (local values trace to their raw file)."""
        for snap, other in ((a, b), (b, a)):
            note = self.con.execute("SELECT note FROM snapshots WHERE snapshot_id = ?", [snap]).fetchone()[0]
            data = json.loads(note) if note else {}
            data["paired_snapshot"] = other
            self.con.execute("UPDATE snapshots SET note = ? WHERE snapshot_id = ?",
                             [json.dumps(data, sort_keys=True), snap])


def _add_cov(acc: dict[str, int], cov: Mapping[str, int]) -> None:
    for c, n in cov.items():
        acc[c] = acc.get(c, 0) + n


def _warn_zero_coverage(label: str, cov: Mapping[str, int], rows: int, warnings: list[str]) -> None:
    """Rule: a column that is null for every returned row is most likely an unknown (misspelled) column."""
    dead = [c for c, n in cov.items() if n == 0]
    if rows and dead:
        warnings.append(f"{label}: zero coverage for {', '.join(dead)}")


# ----------------------------------------------------------------------------------------------- refresh

def refresh_universe(cfg: Config, con: Any, client: Any, *, with_history: bool = False, history_batch: int = 2000,
                     as_of: dt.date | None = None) -> dict[str, Any]:
    """Pull the TradingView universe (USD + local mode per filter) and upsert securities, market_daily,
    fundamentals_current and optionally fundamentals_annual.

    Rules: securities not seen are marked inactive (never deleted) only after every universe filter returned a
    complete result (see module docstring; otherwise run status 'partial'); on Blocked nothing is deactivated, the
    run is recorded as 'blocked' and the exception re-raised; KeyboardInterrupt etc. are recorded as 'interrupted'.
    Securities, market, fundamentals and deactivation are written in one transaction.
    """
    as_of = as_of or dt.datetime.now(dt.timezone.utc).date()
    run_id = f"{SOURCE_ID}:{uuid.uuid4().hex[:12]}"
    started = store.now_utc()
    warnings: list[str] = []
    fetcher = _Fetcher(cfg, con, client, warnings)
    summary: dict[str, Any] = {
        "run_id": run_id, "as_of": as_of.isoformat(), "rows_per_filter": {}, "securities_upserted": 0,
        "deactivated": 0, "market_rows": 0, "fundamentals_rows": 0, "history_rows": 0, "history_batches": 0,
        "history_length_mismatches": 0, "requests": 0, "deactivation_skipped": [],
        "column_coverage": {"universe_usd": {}, "universe_local": {}}, "warnings": warnings,
    }

    def finish(status: str, note: str | None) -> None:
        summary["requests"] = fetcher.requests
        con.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
                    [run_id, "tradingview_scanner.refresh_universe", started, store.now_utc(), status,
                     fetcher.requests, note])

    try:
        # 1) fetch every filter in both modes before writing anything
        merged: dict[str, tuple[Row | None, Row | None, str]] = {}
        universe_snaps: list[str] = []
        incomplete: list[str] = summary["deactivation_skipped"]
        for name, filters in UNIVERSE_FILTERS.items():
            got: dict[str, tuple[list[Row], str]] = {}
            for mode, local in (("usd", False), ("local", True)):
                kind = f"universe_{name}_{mode}"
                rows, snap, cov = fetcher.fetch(kind, build_scan_body(filters, CURRENT_COLUMNS, local),
                                                CURRENT_COLUMNS)
                problem = fetcher.completeness_problem(kind, snap, len(rows))
                if problem:
                    incomplete.append(problem)
                got[mode] = (rows, snap)
                universe_snaps.append(snap)
                _add_cov(summary["column_coverage"][f"universe_{mode}"], cov)
            (usd_rows, usd_snap), (loc_rows, loc_snap) = got["usd"], got["local"]
            fetcher.pair(usd_snap, loc_snap)
            summary["rows_per_filter"][name] = {"usd": len(usd_rows), "local": len(loc_rows)}
            usd_by = {r["s"]: r for r in usd_rows}
            loc_by = {r["s"]: r for r in loc_rows}
            only = sorted(set(usd_by) ^ set(loc_by))
            if only:
                warnings.append(f"universe_{name}: {len(only)} symbols present in only one mode, e.g. {only[:5]}")
            for s in usd_by.keys() | loc_by.keys():
                merged[s] = (usd_by.get(s), loc_by.get(s), usd_snap if s in usd_by else loc_snap)

        for mode in ("usd", "local"):
            n = sum(v[mode] for v in summary["rows_per_filter"].values())
            _warn_zero_coverage(f"universe_{mode}", summary["column_coverage"][f"universe_{mode}"], n, warnings)

        # 2-4) write securities, market_daily, fundamentals_current, then deactivate, in one transaction
        con.begin()
        try:
            _write_current(con, merged, as_of, summary)
            if incomplete:
                warnings.append("deactivation skipped (partial universe pull): " + "; ".join(incomplete))
            else:  # complete pull of every filter: deactivate lines not seen in this refresh (never delete)
                ph = ",".join("?" for _ in universe_snaps)
                where = f"active AND (last_seen_snapshot IS NULL OR last_seen_snapshot NOT IN ({ph}))"
                summary["deactivated"] = con.execute(f"SELECT count(*) FROM securities WHERE {where}",
                                                     universe_snaps).fetchone()[0]
                con.execute(f"UPDATE securities SET active = false WHERE {where}", universe_snaps)
            con.commit()
        except BaseException:
            con.rollback()
            raise

        # 5) optional local-currency annual history in explicit-ticker batches
        if with_history:
            fund_ccy = {s: _txt((loc or {}).get("fundamental_currency_code")) for s, (_, loc, _) in merged.items()}
            _write_history(con, fetcher, sorted(merged), fund_ccy, max(1, history_batch), summary)
    except Blocked as e:
        finish("blocked", str(e))
        raise
    except Exception as e:
        finish("error", f"{type(e).__name__}: {e}")
        raise
    except BaseException as e:  # KeyboardInterrupt, SystemExit: journal it, then let it propagate
        finish("interrupted", type(e).__name__)
        raise
    summary["status"] = "partial" if summary["deactivation_skipped"] else "ok"
    counts = {k: v for k, v in summary.items()
              if k not in ("column_coverage", "warnings", "rows_per_filter", "deactivation_skipped")}
    finish(summary["status"], json.dumps({**counts, "warnings": len(warnings)}, sort_keys=True))
    return summary


def _write_current(con: Any, merged: Mapping[str, tuple[Row | None, Row | None, str]], as_of: dt.date,
                   summary: dict[str, Any]) -> None:
    """Securities from the USD row (identity) + LOCAL row (fundamental currency); market and current fundamentals
    take USD values only from the USD row and local values only from the local row. Every CURRENT_FUNDAMENTALS
    metric gets a row per seen security, NULL values included (missing today must not fall back to yesterday)."""
    now = store.now_utc()
    first_seen = dict(con.execute("SELECT security_id, first_seen_snapshot FROM securities").fetchall())
    sec_rows, mkt_rows, fund_rows = [], [], []
    for s in sorted(merged):
        usd, loc, snap = merged[s]
        ident = usd or loc or {}
        u, lo = usd or {}, loc or {}
        exchange, symbol = _split_symbol(s)
        isin = _txt(ident.get("isin"))
        fund_ccy = _txt(lo.get("fundamental_currency_code"))  # never the default-mode code (it says USD)
        price_ccy = _txt(u.get("currency")) or _txt(lo.get("currency"))
        is_primary = ident.get("is_primary")
        sec_rows.append([
            s, exchange, symbol, _txt(ident.get("description")), isin, _txt(ident.get("country")),
            _txt(ident.get("type")), _txt(ident.get("subtype")), is_primary if isinstance(is_primary, bool) else None,
            price_ccy, fund_ccy, _txt(ident.get("sector")), _txt(ident.get("industry")), store.company_key(isin, s),
            first_seen.get(s) or snap, snap, now, True])
        mkt_rows.append([
            s, as_of, _num(ident.get("close")), price_ccy, _num(ident.get("volume")),
            _num(ident.get("average_volume_10d_calc")), _num(u.get("market_cap_basic")),
            _num(lo.get("market_cap_basic")), fx_local_to_usd(usd, loc), _num(ident.get("total_shares_outstanding")),
            _num(ident.get("float_shares_outstanding")), snap])
        fy_end = epoch_to_date(lo.get("fiscal_period_end_fy") if loc else u.get("fiscal_period_end_fy"))
        for metric, period, col in CURRENT_FUNDAMENTALS:
            v_usd, v_loc = _num(u.get(col)), _num(lo.get(col))
            fund_rows.append([s, as_of, metric, period, v_usd, v_loc, fund_ccy,
                              fy_end if period == "fy" else None, snap])
    summary["securities_upserted"] = _upsert(con, "securities", [
        "security_id", "exchange", "symbol", "name", "isin", "country", "tv_type", "tv_subtype", "is_primary",
        "price_currency", "fundamental_currency", "sector", "industry", "company_key", "first_seen_snapshot",
        "last_seen_snapshot", "last_seen_at", "active"], sec_rows)
    summary["market_rows"] = _upsert(con, "market_daily", [
        "security_id", "as_of", "close", "price_currency", "volume", "avg_volume_10d", "market_cap_usd",
        "market_cap_local", "fx_local_to_usd", "shares_outstanding", "float_shares", "snapshot_id"], mkt_rows)
    summary["fundamentals_rows"] = _upsert(con, "fundamentals_current", [
        "security_id", "as_of", "metric", "period", "value_usd", "value_local", "currency_local",
        "fiscal_period_end", "snapshot_id"], fund_rows)


def _write_history(con: Any, fetcher: _Fetcher, security_ids: Sequence[str], fund_ccy: Mapping[str, str | None],
                   batch_size: int, summary: dict[str, Any]) -> None:
    """Local-mode *_fy_h arrays -> fundamentals_annual (value_local + securities.fundamental_currency)."""
    columns = [HISTORY_PERIOD_COLUMN] + HISTORY_COLUMNS
    coverage: dict[str, int] = {}
    summary["column_coverage"]["history"] = coverage
    mismatches: list[str] = []
    total = returned = 0
    for i in range(0, len(security_ids), batch_size):
        batch = list(security_ids[i:i + batch_size])
        rows, snap, cov = fetcher.fetch("history_local_batch", build_scan_body(None, columns, True, tickers=batch),
                                        columns)
        summary["history_batches"] += 1
        returned += len(rows)
        _add_cov(coverage, cov)
        out: dict[tuple[str, int, str], list[Any]] = {}
        for r in rows:
            s, periods = r["s"], r[HISTORY_PERIOD_COLUMN]
            for metric in HISTORY_METRICS:
                values = r[f"{metric}_fy_h"]
                if isinstance(periods, list) and isinstance(values, list) and len(periods) != len(values):
                    mismatches.append(f"{s}:{metric} {len(periods)}/{len(values)}")
                for fy, v in zip_history(periods, values):
                    out.setdefault((s, fy, metric), [s, fy, metric, v, fund_ccy.get(s), snap])  # index 0 wins
        total += _upsert(con, "fundamentals_annual",
                         ["security_id", "fiscal_year", "metric", "value_local", "currency_local", "snapshot_id"],
                         list(out.values()))
    _warn_zero_coverage("history", coverage, returned, summary["warnings"])
    summary["history_rows"] = total
    summary["history_length_mismatches"] = len(mismatches)
    if mismatches:
        summary["warnings"].append(
            f"history: {len(mismatches)} arrays length-mismatched with fiscal_period_fy_h (zipped prefix), "
            f"e.g. {mismatches[:5]}")
