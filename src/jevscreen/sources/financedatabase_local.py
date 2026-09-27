"""Import local FinanceDatabase business summaries and attach them to TradingView securities.

Data rules implemented here (docs/DATA_RULES.md):
- Source ``financedatabase_local``: summaries are Yahoo ``longBusinessSummary`` text, licence tier gray-private.
  Personal use only; nothing written here may ship in a public artefact.
- The FinanceDatabase DuckDB (JEVSCREEN_FD_DUCKDB, default <home>/financedatabase.duckdb) is opened
  ``read_only=True`` and never written.
- Every stored description row carries the snapshot_id of this import; missing values stay NULL.
- One FD summary may attach to several lines of the same ``company_key`` but never to two company_keys; one summary
  TEXT (FD copies parent/other-company summaries onto unrelated rows) may attach to several company_keys only when
  their TradingView names are identical after normalisation (the same company listed twice, e.g. ADR + ordinary).
- Each import replaces this source's rows for the securities it considered, so a corrected match removes a stale one.
- Bulk file path (quickstart): download_equities fetches FinanceDatabase's compression/equities.bz2 (one request,
  about 15 MB) from FD_MIRRORS in order, conditional on the stored ETag / Last-Modified, raw kept under
  <home>/raw/financedatabase/<date>/equities-<sha12>.bz2 (snapshot kind fd_equities_bz2); import_descriptions(
  fd_csv=PATH) reads it (load_fd_csv_bz2) with snapshot kind fd_summaries_bz2. The file has no security-type column:
  preferred, warrant, unit and rights lines are skipped by ticker suffix (NON_COMMON_SUFFIX). Its stale deletion
  removes only rows from an EARLIER fd_summaries_bz2 snapshot, never rows of the DuckDB import. Both need a recorded
  'yes' for gray-sources (consent.require_gray_sources) before any request or file read.
"""
from __future__ import annotations

import bz2
import contextlib
import csv
import datetime as dt
import difflib
import hashlib
import io
import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import duckdb

from .. import store
from ..config import Config

SOURCE_ID = "financedatabase_local"
KIND = "fd_summaries"
BZ2_KIND = "fd_summaries_bz2"          # snapshot of an import from the bulk equities.bz2 file
DOWNLOAD_KIND = "fd_equities_bz2"      # snapshot of the download itself
FD_MIRRORS: tuple[str, ...] = (
    "https://raw.githubusercontent.com/JerBouma/FinanceDatabase/main/compression/equities.bz2",
    "https://cdn.jsdelivr.net/gh/JerBouma/FinanceDatabase@main/compression/equities.bz2",
)
MIRROR_DEADLINE_S = 90.0               # per mirror (slow networks); then the next mirror
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
# The bulk file has no security-type column: these ticker endings (before the Yahoo market suffix) are preferred
# shares, warrants, units and rights (measured on the file). SET's '-R' is a Thai NVDR of the ordinary line: kept.
NON_COMMON_SUFFIX = re.compile(r"(?:-P[A-Z]|-PR|\.PR|_P|-W|-WT|-U|-R)$")
MIN_SUMMARY_CHARS = 40
EQUITY_TYPES = frozenset({"common_stock", "adr", "reit", "preferred"})
NAME_THRESHOLD = 0.5
ISIN_NAME_FLOOR = 0.3   # cross-venue ISIN matches below this name similarity are rejected (FD id_isin is often wrong)

# FinanceDatabase / Yahoo exchange code -> TradingView exchange codes (first entry is the usual venue).
# Verified against a 2026-09 TradingView global dump by comparing ISIN-matched listings.
EXCHANGE_MAP: dict[str, tuple[str, ...]] = {
    # United States
    "NMS": ("NASDAQ",), "NGM": ("NASDAQ",), "NCM": ("NASDAQ",),
    "NYQ": ("NYSE",), "ASE": ("AMEX", "NYSE"), "PCX": ("AMEX", "NYSE"), "BTS": ("CBOE",),
    "PNK": ("OTC",), "OQB": ("OTC",), "OQX": ("OTC",), "OBB": ("OTC",), "OEM": ("OTC",), "OID": ("OTC",),
    # Asia-Pacific
    "JPX": ("TSE",), "FKA": ("FSE",), "SAP": ("SAPSE",),
    "HKG": ("HKEX",), "SHH": ("SSE",), "SHZ": ("SZSE",),
    "KSC": ("KRX",), "KOE": ("KRX",), "TAI": ("TWSE",), "TWO": ("TPEX",),
    "NSE": ("NSE",), "NSI": ("NSE",), "BSE": ("BSE",),
    "ASX": ("ASX",), "NZE": ("NZX",), "SET": ("SET",), "JKT": ("IDX",), "KLS": ("MYX",), "SES": ("SGX",),
    "CSE": ("CSELK",),
    # Europe
    "LSE": ("LSE",), "IOB": ("LSE", "LSIN"), "AQS": ("AQUIS",),
    "GER": ("XETR",), "FRA": ("FWB",), "STU": ("SWB",), "MUN": ("MUN",), "DUS": ("DUS",),
    "HAM": ("HAM",), "HAN": ("HAN",),
    "PAR": ("EURONEXT",), "AMS": ("EURONEXT",), "BRU": ("EURONEXT",), "LIS": ("EURONEXT",), "ISE": ("EURONEXT",),
    "MIL": ("MIL",), "TLO": ("EUROTLX",), "MCE": ("BME",),
    "STO": ("OMXSTO",), "CPH": ("OMXCOP",), "HEL": ("OMXHEX",), "ICE": ("OMXICE",),
    "RIS": ("OMXRSE",), "TAL": ("OMXTSE",), "LIT": ("OMXVSE",),
    "OSL": ("OSL", "EURONEXT"), "EBS": ("SIX",), "VIE": ("VIE",), "WSE": ("GPW",),
    "ATH": ("ATHEX",), "BUD": ("BET",), "PRA": ("PSECZ",), "IST": ("BIST",), "MCX": ("RUS",),
    # Americas
    "TOR": ("TSX",), "VAN": ("TSXV",), "CNQ": ("CSE",), "NEO": ("NEO",),
    "SAO": ("BMFBOVESPA",), "MEX": ("BMV",), "BUE": ("BCBA",), "SGO": ("BCS",),
    # Middle East / Africa
    "TLV": ("TASE",), "JNB": ("JSE",), "SAU": ("TADAWUL",), "DOH": ("QSE",), "CAI": ("EGX",),
}

# Yahoo ticker suffix -> FinanceDatabase exchange code (used to strip suffixes and to infer a missing exchange).
SUFFIX_TO_FD: dict[str, str] = {
    "HK": "HKG", "KS": "KSC", "KQ": "KOE", "T": "JPX", "TW": "TAI", "TWO": "TWO", "NS": "NSE", "BO": "BSE",
    "SS": "SHH", "SZ": "SHZ", "AX": "ASX", "NZ": "NZE", "BK": "SET", "JK": "JKT", "KL": "KLS", "SI": "SES",
    "CM": "CSE", "L": "LSE", "IL": "IOB", "AQ": "AQS", "DE": "GER", "F": "FRA", "SG": "STU", "MU": "MUN",
    "DU": "DUS", "HM": "HAM", "HA": "HAN", "BE": "BER", "PA": "PAR", "AS": "AMS", "BR": "BRU", "LS": "LIS",
    "IR": "ISE", "MI": "MIL", "TI": "TLO", "MC": "MCE", "ST": "STO", "CO": "CPH", "HE": "HEL", "IC": "ICE",
    "RG": "RIS", "TL": "TAL", "VS": "LIT", "OL": "OSL", "SW": "EBS", "VI": "VIE", "WA": "WSE", "AT": "ATH",
    "BD": "BUD", "PR": "PRA", "IS": "IST", "ME": "MCX", "TO": "TOR", "V": "VAN", "CN": "CNQ", "NE": "NEO",
    "SA": "SAO", "MX": "MEX", "BA": "BUE", "SN": "SGO", "TA": "TLV", "JO": "JNB", "SR": "SAU", "QA": "DOH",
    "CA": "CAI", "S": "SAP",
}
_US_FD = frozenset({"NMS", "NGM", "NCM", "NYQ", "ASE", "PCX", "BTS", "PNK", "OQB", "OQX", "OBB", "OEM", "OID"})
# Preferred replacement for Yahoo's '-' class separator, per TradingView venue style (BRK.B, VOLV_B, BAJAJ_AUTO).
_UNDERSCORE_FD = frozenset({"STO", "CPH", "HEL", "ICE", "NSE", "NSI", "BSE"})
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


def _fd_exchange(fd_ticker: str, fd_exchange: str | None) -> str | None:
    """FD exchange code, inferred from the Yahoo suffix when the row has none."""
    if fd_exchange:
        return fd_exchange.upper()
    if "." in fd_ticker:
        return SUFFIX_TO_FD.get(fd_ticker.rsplit(".", 1)[1].upper())
    return None


def _strip_suffix(fd_ticker: str, fd_ex: str | None) -> str:
    """Remove a Yahoo market suffix ('.HK', '.TWO', ...). US tickers carry no suffix and are kept whole."""
    t = fd_ticker.strip().upper()
    if fd_ex in _US_FD or "." not in t:
        return t
    head, suffix = t.rsplit(".", 1)
    return head if suffix in SUFFIX_TO_FD else t


def tv_symbol_candidates(fd_ticker: str, fd_exchange: str | None) -> list[str]:
    """Candidate TradingView 'EXCH:SYM' ids for a FinanceDatabase ticker, most likely first.

    Rules: strip the Yahoo suffix; HK codes lose leading zeros ('0700' -> '700'); KR/TW/CN codes keep them;
    Yahoo's '-' class separator is tried as '.', '_' and '/' (BRK-B -> BRK.B, VOLV-B -> VOLV_B, ABR-PA -> ABR/PA);
    Aquis drops '-GB'; Colombo 'AAFN0000' -> 'AAFN.N0000'; Thai NVDR '-R' also tries the ordinary line; LSE two-letter codes also try TradingView's
    trailing-dot form ('AO' -> 'AO.'). Unknown exchanges yield no candidates.
    """
    if not fd_ticker:
        return []
    fd_ex = _fd_exchange(fd_ticker, fd_exchange)
    tv_exchanges = EXCHANGE_MAP.get(fd_ex or "", ())
    if not tv_exchanges:
        return []
    base = _strip_suffix(fd_ticker, fd_ex)
    if fd_ex == "HKG" and base.isdigit():
        base = base.lstrip("0") or "0"
    if fd_ex == "AQS":
        base = re.sub(r"-GB$", "", base)
    bases = [base]
    if fd_ex == "CSE" and re.fullmatch(r"[A-Z]+[NXPRW]\d{4}", base):   # Colombo 'AAFN0000' -> 'AAFN.N0000'
        bases = [f"{base[:-5]}.{base[-5:]}"]
    if fd_ex == "SET" and base.endswith("-R"):
        bases.append(base[:-2])
    if "-" in base:
        seps = ["_", ".", "/"] if fd_ex in _UNDERSCORE_FD else [".", "/", "_"]
        bases += [base.replace("-", s) for s in seps]
    if fd_ex in {"LSE", "IOB"} and len(base) <= 2 and base.isalpha():
        bases.append(base + ".")
    out: list[str] = []
    for ex in tv_exchanges:
        for b in bases:
            cand = f"{ex}:{b}"
            if b and cand not in out:
                out.append(cand)
    return out


# Tokens removed before comparing names: legal forms, share-class and depositary wording, filler words.
_NOISE = frozenset("""
inc incorporated corp corporation co company cos ltd limited llc lp plc sa sas spa ag nv bv se kgaa gmbh ab asa as
oyj oy aps bhd berhad tbk pt pcl public jsc pjsc ojsc sab de cv the and of holding holdings group grp
class cl ordinary ord shares share sponsored unsponsored adr ads depositary receipt receipts registered reg
pfd preferred common stock units unit npv new
""".split())
# Extra tokens that still allow the subset boost ('Acme' vs 'Acme International'); anything else, e.g. a country
# ('ArcelorMittal South Africa') or a second name ('CarrefourSA Carrefour Sabanci ...'), means another company.
_GENERIC = frozenset("""
international intl industries industry industrial technologies technology tech enterprises enterprise systems
global worldwide services solutions
""".split())


def _name_tokens(name: str | None) -> list[str]:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ")
    raw = re.sub(r"[^a-z0-9]+", " ", s).split()
    merged: list[str] = []                                   # 'y a c' -> 'yac', 's a' -> 'sa' (acronyms with dots)
    for t in raw:
        if len(t) == 1 and t.isalpha() and merged and merged[-1].endswith("\0"):
            merged[-1] = merged[-1][:-1] + t + "\0"
        else:
            merged.append(t + "\0" if len(t) == 1 and t.isalpha() else t)
    tokens = [t.rstrip("\0") for t in merged]
    return [t for t in tokens if t not in _NOISE and len(t) > 1]


def name_similarity(a: str | None, b: str | None) -> float:
    """Company-name similarity in [0, 1].

    Both names are ASCII-folded, lower-cased and stripped of punctuation, legal suffixes (Inc, Corp, Ltd, PLC, SA,
    AG, NV, Holdings, Group...), share-class and depositary wording (Class A, Sponsored ADR, Pfd...) and
    single-letter tokens (runs of single letters are first merged, so 'Y.A.C.' == 'YAC' and 'S.A.' is removed).
    Score = 0.5 * token Jaccard + 0.5 * difflib.SequenceMatcher ratio on the space-free strings; identical
    space-free strings ('Jeju Air' / 'JEJUAIR') score 1.0; if every token of the shorter name appears in the longer
    one, the shorter has >= 6 letters and every extra token is a generic business word (International, Industries,
    Technologies, ...), the score is at least 0.85. A name that normalises to nothing scores 0.0.
    """
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return 0.0
    ja, jb = "".join(ta), "".join(tb)
    if ja == jb:
        return 1.0
    sa, sb = set(ta), set(tb)
    jaccard = len(sa & sb) / len(sa | sb)
    seq = difflib.SequenceMatcher(None, ja, jb).ratio()
    score = 0.5 * jaccard + 0.5 * seq
    short, long_ = (sa, sb) if len(ja) <= len(jb) else (sb, sa)
    if short <= long_ and len(min(ja, jb, key=len)) >= 6 and (long_ - short) <= _GENERIC:
        score = max(score, 0.85)
    return round(min(score, 1.0), 4)


@dataclass(frozen=True)
class Match:
    """One FD summary attached to one TradingView line."""
    security_id: str
    company_key: str
    fd_listing_id: str
    fd_ticker: str
    method: str          # 'isin' | 'ticker_exchange' | 'ticker_exchange_name_checked'
    score: float
    name_similarity: float
    summary: str
    rank: int = 0        # position of the TV id in tv_symbol_candidates (tie-break); 0 for ISIN matches
    isin_conflict: bool = False   # ticker match whose FD id_isin differs from the security's ISIN


def _clean_isin(value: Any) -> str | None:
    s = (value or "").strip().upper() if isinstance(value, str) else ""
    return s if _ISIN_RE.match(s) else None


def match(fd_rows: Iterable[Mapping[str, Any]], securities: Iterable[Mapping[str, Any]], *,
          report: dict[str, Any] | None = None) -> list[Match]:
    """Match FD rows to TradingView securities; returns at most one Match per security_id.

    fd_rows need: listing_id, ticker, exchange, name, id_isin, summary.
    securities need: security_id, name, isin, company_key.

    Methods and scores (ISIN is tried first; a ticker candidate already matched by ISIN is not re-scored):
    - 'isin': FD id_isin (or an ISIN-shaped ticker) equals the security ISIN. Score 1.0 when the ticker+exchange
      also agree, else 0.6 + 0.3 * name_similarity (FD ISINs are sometimes the ADR/foreign line's ISIN); such a
      cross-venue match needs name_similarity >= ISIN_NAME_FLOOR (FD id_isin values are often wrong).
    - 'ticker_exchange': a tv_symbol_candidates id exists and the normalised names are identical. Score 0.95.
    - 'ticker_exchange_name_checked': candidate exists and 0.5 <= name_similarity < 1. Score 0.5 + 0.4 * sim.
      Below 0.5 the candidate is rejected (same ticker, different company).
    - When the FD id_isin and the security ISIN are both present and differ, a ticker match is kept only with
      identical names (ADR rows carrying the ordinary ISIN) and is flagged ``isin_conflict``; otherwise rejected.
    Per FD row, all matched lines must share one company_key: the company with the best score wins and the others
    are reported as conflicts. Per security, the best FD row wins (score, candidate rank, longer summary, id).
    Per summary text, company_keys whose TV names differ from the best company's are dropped ('text_conflicts';
    best = no isin_conflict, then score). If ``report`` is given it receives 'conflicts', 'name_rejections' and
    'text_conflicts' lists.
    """
    secs = list(securities)
    by_id = {s["security_id"]: s for s in secs}
    by_isin: dict[str, list[Mapping[str, Any]]] = {}
    for s in secs:
        isin = _clean_isin(s.get("isin"))
        if isin:
            by_isin.setdefault(isin, []).append(s)
    conflicts: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    best: dict[str, Match] = {}

    for row in fd_rows:
        ticker = row.get("ticker") or ""
        fd_ex = _fd_exchange(ticker, row.get("exchange"))
        cands = tv_symbol_candidates(ticker, fd_ex)
        cand_rank = {c: i for i, c in enumerate(cands)}
        fd_isin = _clean_isin(row.get("id_isin")) or _clean_isin(_strip_suffix(ticker, fd_ex))
        found: dict[str, Match] = {}

        def add(sec: Mapping[str, Any], method: str, score: float, sim: float, rank: int,
                isin_conflict: bool = False) -> None:
            found[sec["security_id"]] = Match(sec["security_id"], sec["company_key"], row["listing_id"], ticker,
                                              method, round(score, 4), sim, row["summary"], rank, isin_conflict)

        def reject(sec: Mapping[str, Any], sim: float, reason: str) -> None:
            rejections.append({"fd_listing_id": row["listing_id"], "fd_name": row.get("name"),
                               "security_id": sec["security_id"], "tv_name": sec.get("name"), "similarity": sim,
                               "reason": reason})

        for sec in by_isin.get(fd_isin, []) if fd_isin else []:
            sim = name_similarity(row.get("name"), sec.get("name"))
            same_venue = sec["security_id"] in cand_rank
            if same_venue:
                add(sec, "isin", 1.0, sim, cand_rank[sec["security_id"]])
            elif sim >= ISIN_NAME_FLOOR:
                add(sec, "isin", 0.6 + 0.3 * sim, sim, 0)
            else:
                reject(sec, sim, "isin_name_floor")
        for rank, cand in enumerate(cands):
            sec = by_id.get(cand)
            if sec is None or cand in found:
                continue
            sim = name_similarity(row.get("name"), sec.get("name"))
            sec_isin = _clean_isin(sec.get("isin"))
            conflict = bool(fd_isin and sec_isin and fd_isin != sec_isin)
            if conflict and sim < 1.0:
                reject(sec, sim, "isin_conflict")
            elif sim >= 1.0:
                add(sec, "ticker_exchange", 0.95, sim, rank, conflict)
            elif sim >= NAME_THRESHOLD:
                add(sec, "ticker_exchange_name_checked", 0.5 + 0.4 * sim, sim, rank)
            else:
                reject(sec, sim, "name")
        if not found:
            continue

        by_company: dict[str, list[Match]] = {}
        for m in found.values():
            by_company.setdefault(m.company_key, []).append(m)
        ranked = sorted(by_company, key=lambda k: (-max(m.score for m in by_company[k]),
                                                   min(m.rank for m in by_company[k]), k))
        winner = ranked[0]
        if len(ranked) > 1:
            conflicts.append({"fd_listing_id": row["listing_id"], "fd_ticker": ticker, "chosen": winner,
                              "rejected": ranked[1:],
                              "scores": {k: max(m.score for m in by_company[k]) for k in ranked}})
        for m in by_company[winner]:
            cur = best.get(m.security_id)
            if cur is None or _order(m) < _order(cur):
                best[m.security_id] = m

    text_conflicts = _drop_shared_texts(best, by_id)
    if report is not None:
        report["conflicts"] = conflicts
        report["name_rejections"] = rejections
        report["text_conflicts"] = text_conflicts
    return [best[k] for k in sorted(best)]


def _drop_shared_texts(best: dict[str, Match], by_id: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Rule: one summary text belongs to one company. When a text sits on several company_keys, keep the best
    company (no isin_conflict first, then _order) plus any company whose TV name is identical to it (same company,
    different ISIN line); drop the others from ``best`` in place and return them as conflicts."""
    by_text: dict[str, dict[str, list[Match]]] = {}
    for m in best.values():
        by_text.setdefault(m.summary, {}).setdefault(m.company_key, []).append(m)
    out: list[dict[str, Any]] = []
    for companies in by_text.values():
        if len(companies) < 2:
            continue
        top = {k: min(ms, key=lambda m: (m.isin_conflict, _order(m))) for k, ms in companies.items()}
        winner = min(top, key=lambda k: (top[k].isin_conflict, _order(top[k]), k))
        win_name = by_id[top[winner].security_id].get("name")
        dropped = [k for k in top if k != winner
                   and name_similarity(by_id[top[k].security_id].get("name"), win_name) < 1.0]
        for k in dropped:
            for m in companies[k]:
                del best[m.security_id]
        if dropped:
            out.append({"text_sha256": hashlib.sha256(top[winner].summary.encode("utf-8")).hexdigest(),
                        "chosen": winner, "rejected": sorted(dropped),
                        "security_ids": sorted(m.security_id for k in dropped for m in companies[k])})
    return out


def _order(m: Match) -> tuple:
    return (-m.score, m.rank, -len(m.summary), m.fd_listing_id)


def _load_fd_rows(path: Path) -> tuple[list[dict[str, Any]], dict[str, int], dt.datetime | None]:
    """Read eligible FD rows read-only: equity types, not delisted, summary >= MIN_SUMMARY_CHARS characters."""
    con = duckdb.connect(str(path), read_only=True)
    try:
        total, max_obs = con.execute(
            "SELECT count(*), max(observed_date) FROM fd_listing_attributes").fetchone()
        cur = con.execute(
            """SELECT listing_id, ticker, exchange, name, id_isin, trim(summary) AS summary,
                      lower(coalesce(security_type, '')) AS security_type, coalesce(is_delisted, false) AS delisted
               FROM fd_listing_attributes ORDER BY listing_id""")
        cols = [d[0] for d in cur.description]
        raw = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        con.close()
    skipped = {"delisted": 0, "non_equity": 0, "short_summary": 0}
    rows: list[dict[str, Any]] = []
    for r in raw:
        if r["delisted"]:
            skipped["delisted"] += 1
        elif r["security_type"] not in EQUITY_TYPES:
            skipped["non_equity"] += 1
        elif len(r["summary"] or "") < MIN_SUMMARY_CHARS:
            skipped["short_summary"] += 1
        else:
            rows.append(r)
    skipped["fd_rows_total"] = int(total)
    fetched = dt.datetime.combine(max_obs, dt.time()) if isinstance(max_obs, dt.date) else None
    return rows, skipped, fetched


def _load_securities(con: duckdb.DuckDBPyConnection, only_universe: bool) -> list[dict[str, Any]]:
    """Securities to match against; with only_universe, every line whose company_key is in the universe view."""
    where = "WHERE company_key IN (SELECT company_key FROM universe)" if only_universe else ""
    cur = con.execute(f"SELECT security_id, exchange, symbol, name, isin, company_key FROM securities {where}")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _write(con: duckdb.DuckDBPyConnection, path: Path, only_universe: bool, blob: bytes, matches: Sequence[Match],
           report: dict[str, Any], started: dt.datetime, fetched_at: dt.datetime | None) -> str:
    """Snapshot + replace this source's descriptions for the considered securities (same WHERE as
    _load_securities), so matches that disappeared (fixed matcher, changed FD data) do not linger."""
    snapshot_id = store.record_snapshot(
        con, source_id=SOURCE_ID, kind=KIND,
        request={"fd_duckdb": str(path), "table": "fd_listing_attributes", "only_universe": only_universe,
                 "min_summary_chars": MIN_SUMMARY_CHARS, "equity_types": sorted(EQUITY_TYPES)},
        raw_path=str(path), raw_sha256=store.sha256(blob), raw_bytes=len(blob), rows=len(matches),
        duration_s=(store.now_utc() - started).total_seconds(), fetched_at=fetched_at,
        note=f"{len(report['conflicts'])} company conflicts resolved by best score; "
             f"{len(report['text_conflicts'])} shared-text conflicts dropped")
    considered = ("security_id IN (SELECT security_id FROM securities "
                  "WHERE company_key IN (SELECT company_key FROM universe))") if only_universe else "TRUE"
    keep = {m.security_id for m in matches}
    stale = [r[0] for r in con.execute(
        f"SELECT security_id FROM descriptions WHERE source_id = ? AND {considered}", [SOURCE_ID]).fetchall()
        if r[0] not in keep]
    for i in range(0, len(stale), 1000):
        part = stale[i:i + 1000]
        con.execute(f"DELETE FROM descriptions WHERE source_id = ? AND security_id IN ({','.join('?' * len(part))})",
                    [SOURCE_ID, *part])
    report["stale_removed"] = len(stale)
    cols = ("security_id", "source_id", "company_key", "text", "text_sha256", "lang", "source_url", "fetched_at",
            "match_method", "match_score", "snapshot_id")
    store.upsert_many(con, "descriptions", cols, [
        (m.security_id, SOURCE_ID, m.company_key, m.summary, hashlib.sha256(m.summary.encode("utf-8")).hexdigest(),
         "en", None, fetched_at, m.method, m.score, snapshot_id) for m in matches])
    return snapshot_id


def import_descriptions(cfg: Config, con: duckdb.DuckDBPyConnection, *, fd_duckdb: str | Path | None = None,
                        only_universe: bool = True, fd_csv: str | Path | None = None) -> dict[str, Any]:
    """Import FD summaries into ``descriptions`` (source financedatabase_local, gray-private) with one snapshot.

    The snapshot's raw_path is the FD DuckDB file, raw_sha256 the sha256 of the eligible summaries joined with '\\n'
    in listing_id order (reproducible without copying Yahoo text), fetched_at the max FD observed_date.
    Descriptions are written with lang 'en', source_url NULL and the match method/score; this source's rows for
    the considered securities that are no longer matched are deleted in the same transaction.
    fd_csv=PATH imports the bulk equities.bz2 instead (import_bz2: snapshot kind fd_summaries_bz2).
    """
    if fd_csv is not None:
        return import_bz2(cfg, con, fd_csv, only_universe=only_universe)
    path = Path(fd_duckdb or cfg.fd_duckdb)
    started = store.now_utc()
    fd_rows, skipped, fetched_at = _load_fd_rows(path)
    blob = "\n".join(r["summary"] for r in fd_rows).encode("utf-8")
    secs = _load_securities(con, only_universe)
    report: dict[str, Any] = {}
    matches = match(fd_rows, secs, report=report)

    con.begin()
    try:
        snapshot_id = _write(con, path, only_universe, blob, matches, report, started, fetched_at)
        con.commit()
    except BaseException:
        con.rollback()
        raise

    by_method: dict[str, int] = {}
    for m in matches:
        by_method[m.method] = by_method.get(m.method, 0) + 1
    used = {m.fd_listing_id for m in matches}
    universe_total, universe_with = con.execute(
        """SELECT count(DISTINCT u.company_key), count(DISTINCT d.company_key)
           FROM universe u LEFT JOIN descriptions d ON d.company_key = u.company_key AND d.source_id = ?""",
        [SOURCE_ID]).fetchone()
    unmatched = [r for r in fd_rows if r["listing_id"] not in used]
    step = max(1, len(unmatched) // 20)
    return {
        "snapshot_id": snapshot_id,
        "fd_rows_total": skipped["fd_rows_total"],
        "fd_rows_considered": len(fd_rows),
        "skipped": {k: v for k, v in skipped.items() if k != "fd_rows_total"},
        "securities_considered": len(secs),
        "descriptions_written": len(matches),
        "matched_by_method": dict(sorted(by_method.items())),
        "conflicts": len(report["conflicts"]),
        "conflict_samples": report["conflicts"][:10],
        "text_conflicts": len(report["text_conflicts"]),
        "text_conflict_samples": report["text_conflicts"][:10],
        "stale_removed": report["stale_removed"],
        "name_rejections": len(report["name_rejections"]),
        "universe_companies": int(universe_total),
        "universe_companies_with_description": int(universe_with),
        "unmatched_fd_rows": len(unmatched),
        "unmatched_samples": [f"{r['ticker']} ({r['exchange']}) {r['name']}" for r in unmatched[::step][:20]],
    }


# ------------------------------------------------------------------------------------------------ bulk equities.bz2

def _truthy(v: Any) -> bool:
    return str(v or "").strip().lower() in ("true", "1", "yes", "t")


def non_common_ticker(ticker: str, fd_exchange: str | None) -> bool:
    """True for a preferred / warrant / unit / rights line by its ticker ending (the bulk file has no type)."""
    fd_ex = _fd_exchange(ticker, fd_exchange)
    base = _strip_suffix(ticker, fd_ex)
    if fd_ex == "SET" and base.endswith("-R"):
        return False
    return bool(NON_COMMON_SUFFIX.search(base))


def load_fd_csv_bz2(path: str | Path) -> tuple[list[dict[str, Any]], dict[str, int], dt.datetime | None]:
    """Eligible rows of FinanceDatabase's equities.bz2 (a bz2-compressed CSV: symbol, name, summary, ..., exchange,
    ..., isin, ..., delisted): not delisted, summary >= MIN_SUMMARY_CHARS, not a non-common line by ticker.
    Rows come in the shape match() takes (listing_id = the Yahoo symbol, ticker, exchange, name, id_isin, summary,
    security_type 'common_stock'). Returns (rows, skipped counts, None)."""
    raw = Path(path).read_bytes()
    text = bz2.decompress(raw).decode("utf-8", "replace")
    reader = csv.DictReader(io.StringIO(text))
    fields = reader.fieldnames or []
    sym_col = "symbol" if "symbol" in fields else (fields[0] if fields else "symbol")
    skipped = {"delisted": 0, "non_equity": 0, "short_summary": 0, "fd_rows_total": 0}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in reader:
        skipped["fd_rows_total"] += 1
        sym = (r.get(sym_col) or "").strip()
        summary = (r.get("summary") or "").strip()
        exch = (r.get("exchange") or "").strip() or None
        if not sym or sym in seen:
            skipped["non_equity"] += 1
            continue
        if _truthy(r.get("delisted")):
            skipped["delisted"] += 1
        elif len(summary) < MIN_SUMMARY_CHARS:
            skipped["short_summary"] += 1
        elif non_common_ticker(sym, exch):
            skipped["non_equity"] += 1
        else:
            seen.add(sym)
            rows.append({"listing_id": sym, "ticker": sym, "exchange": exch, "name": (r.get("name") or "").strip(),
                         "id_isin": (r.get("isin") or "").strip() or None, "summary": summary,
                         "security_type": "common_stock", "delisted": False})
    rows.sort(key=lambda x: x["listing_id"])
    return rows, skipped, None


def _download_state_path(cfg: Config) -> Path:
    return Path(cfg.raw_dir) / "financedatabase" / "equities-latest.json"


def _download_state(cfg: Config) -> dict[str, Any]:
    try:
        data = json.loads(_download_state_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and Path(str(data.get("path") or "")).is_file() else {}


MIRROR_COOLDOWN_H = 24.0


def _mirror_blocks_path(cfg: Config) -> Path:
    return Path(cfg.raw_dir) / "financedatabase" / "mirror-blocks.json"


def mirror_blocks(cfg: Config) -> dict[str, dict[str, Any]]:
    """Per-host refusals of the mirrors: {host: {'blocked_at' (UTC ISO), 'url', 'http_status', 'reason'}}."""
    try:
        data = json.loads(_mirror_blocks_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def mirror_cooling(cfg: Config, host: str, hours: float = MIRROR_COOLDOWN_H) -> str | None:
    """The time the host refused us when that is less than `hours` ago, else None."""
    b = mirror_blocks(cfg).get(host) or {}
    try:
        at = dt.datetime.fromisoformat(str(b["blocked_at"]))
    except (KeyError, ValueError, TypeError):
        return None
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    return b["blocked_at"] if now - at < dt.timedelta(hours=hours) else None


def _mark_mirror_blocked(cfg: Config, host: str, url: str, status: Any, reason: Any) -> None:
    """Never raises: a refusal that cannot be recorded still ends in the result's errors."""
    with contextlib.suppress(OSError):
        path = _mirror_blocks_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = mirror_blocks(cfg)
        data[host] = {"blocked_at": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(
            timespec="seconds"), "url": url, "http_status": status, "reason": reason}
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)


def download_equities(cfg: Config, client: Any, *, dest: str | Path | None = None,
                      mirrors: Sequence[str] = FD_MIRRORS, max_bytes: int = MAX_DOWNLOAD_BYTES,
                      deadline_s: float = MIRROR_DEADLINE_S) -> dict[str, Any]:
    """Download equities.bz2 from the first mirror that answers (one GET each, conditional on the stored ETag /
    Last-Modified; a 304 reuses the stored file). Needs a recorded 'yes' for gray-sources (ConsentRequired before
    any request). A block (http.Blocked) is a refusal by THAT host: the mirrors are different hosts (GitHub raw,
    jsDelivr), so the next one is still tried once (the client's halt, set by the block, is cleared for it); when no
    mirror delivers, the first http.Blocked is raised to the caller (fetch-fd cooldown marker, exit 2). A refusing
    host is recorded (mirror-blocks.json) even when another mirror delivers, and is not asked again for 24 h (noted
    in 'errors'); when every mirror is refusing or cooling down it is a block. Returns
    {'status': 'ok' | 'not_modified' | 'error' | 'too_large', 'path', 'url', 'bytes', 'sha256', 'last_modified',
    'errors'}."""
    from .. import consent
    consent.require_gray_sources(cfg, SOURCE_ID)
    from ..http import Blocked
    state = _download_state(cfg)
    errors: list[str] = []
    block: Blocked | None = None
    cooling: Blocked | None = None
    asked = False                        # a request went to a host that is not cooling down
    for url in mirrors:
        host = url.split("/")[2]
        since = mirror_cooling(cfg, host)
        if since is not None:
            errors.append(f"{host}: cooling down after a block at {since} UTC")
            cooling = cooling or Blocked(url, None, "cooldown")
            continue
        halt = getattr(client, "halt", None)
        if block is not None and halt is not None and hasattr(halt, "clear"):
            halt.clear()                  # the block was the previous host's; this is another host
        headers: dict[str, str] = {}
        if state and state.get("url") == url and state.get("etag"):
            headers["If-None-Match"] = str(state["etag"])
        if state and state.get("last_modified"):
            headers["If-Modified-Since"] = str(state["last_modified"])
        t0 = time.monotonic()
        asked = True
        try:
            resp = client.get(url, headers=headers, max_bytes=max_bytes + 1, deadline_s=deadline_s)
        except Blocked as e:
            block = block or e
            errors.append(f"{host}: blocked ({e.reason})")
            _mark_mirror_blocked(cfg, host, url, e.status, e.reason)
            continue
        except Exception as e:  # noqa: BLE001 - timeout, DNS, reset: try the next mirror
            errors.append(f"{url.split('/')[2]}: {type(e).__name__}")
            continue
        if resp.status == 304 and state:
            return {"status": "not_modified", "path": state["path"], "url": url, "bytes": state.get("bytes"),
                    "sha256": state.get("sha256"), "last_modified": state.get("last_modified"), "errors": errors,
                    "seconds": round(time.monotonic() - t0, 2)}
        if resp.status != 200:
            errors.append(f"{url.split('/')[2]}: http_{resp.status}")
            continue
        body = resp.body or b""
        if getattr(resp, "truncated", False) or len(body) > max_bytes:
            return {"status": "too_large", "path": None, "url": url, "bytes": len(body), "errors": errors,
                    "note": f"more than {max_bytes} bytes; not saved"}
        if not body.startswith(b"BZh"):
            errors.append(f"{url.split('/')[2]}: not a bz2 file")
            continue
        digest = store.sha256(body)
        day = store.now_utc().strftime("%Y-%m-%d")
        folder = Path(dest) if dest is not None else Path(cfg.raw_dir) / "financedatabase" / day
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"equities-{digest[:12]}.bz2"
        if not path.exists():
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_bytes(body)
            os.replace(tmp, path)
        hdr = {str(k).lower(): v for k, v in (resp.headers or {}).items()}
        info = {"status": "ok", "path": str(path), "url": url, "bytes": len(body), "sha256": digest,
                "etag": hdr.get("etag"), "last_modified": hdr.get("last-modified"), "errors": errors,
                "seconds": round(time.monotonic() - t0, 2)}
        with contextlib.suppress(store.StoreLocked):
            with store.session(cfg, wait_s=120.0) as con:
                store.record_snapshot(con, source_id=SOURCE_ID, kind=DOWNLOAD_KIND,
                                      request={"url": url, "conditional": bool(headers)}, raw_path=str(path),
                                      raw_sha256=digest, raw_bytes=len(body), rows=None,
                                      duration_s=info["seconds"], note=f"last-modified {info['last_modified']}")
        st = _download_state_path(cfg)
        st.parent.mkdir(parents=True, exist_ok=True)
        st.write_text(json.dumps({k: info[k] for k in ("path", "url", "bytes", "sha256", "etag", "last_modified")},
                                 sort_keys=True), encoding="utf-8")
        return info
    if block is not None:
        raise block
    if cooling is not None and not asked:
        raise cooling                    # every mirror is cooling down: still a block, nothing was sent
    return {"status": "error", "path": None, "url": None, "bytes": None, "errors": errors}


def _write_bz2(con: duckdb.DuckDBPyConnection, path: Path, only_universe: bool, digest: str, size: int,
               matches: Sequence[Match], report: dict[str, Any], started: dt.datetime) -> str:
    """Snapshot (kind fd_summaries_bz2) + upsert; stale deletion only of rows from EARLIER bz2 imports."""
    snapshot_id = store.record_snapshot(
        con, source_id=SOURCE_ID, kind=BZ2_KIND,
        request={"fd_csv": path.name, "only_universe": only_universe, "min_summary_chars": MIN_SUMMARY_CHARS},
        raw_path=str(path), raw_sha256=digest, raw_bytes=size, rows=len(matches),
        duration_s=(store.now_utc() - started).total_seconds(),
        note=f"{len(report['conflicts'])} company conflicts resolved by best score; "
             f"{len(report['text_conflicts'])} shared-text conflicts dropped")
    considered = ("security_id IN (SELECT security_id FROM securities "
                  "WHERE company_key IN (SELECT company_key FROM universe))") if only_universe else "TRUE"
    keep = {m.security_id for m in matches}
    stale = [r[0] for r in con.execute(
        f"SELECT d.security_id FROM descriptions d JOIN snapshots n ON n.snapshot_id = d.snapshot_id "
        f"WHERE d.source_id = ? AND n.kind = ? AND {considered.replace('security_id IN', 'd.security_id IN', 1)}",
        [SOURCE_ID, BZ2_KIND]).fetchall() if r[0] not in keep]
    for i in range(0, len(stale), 1000):
        part = stale[i:i + 1000]
        con.execute(f"DELETE FROM descriptions WHERE source_id = ? AND security_id IN ({','.join('?' * len(part))})",
                    [SOURCE_ID, *part])
    report["stale_removed"] = len(stale)
    cols = ("security_id", "source_id", "company_key", "text", "text_sha256", "lang", "source_url", "fetched_at",
            "match_method", "match_score", "snapshot_id")
    now = store.now_utc()
    _bulk_upsert(con, "descriptions", cols, [
        (m.security_id, SOURCE_ID, m.company_key, m.summary, hashlib.sha256(m.summary.encode("utf-8")).hexdigest(),
         "en", None, now, m.method, m.score, snapshot_id) for m in matches])
    return snapshot_id


BULK_ROWS = 500


def _bulk_upsert(con: duckdb.DuckDBPyConnection, table: str, cols: Sequence[str], rows: list[tuple]) -> int:
    """INSERT OR REPLACE in multi-row VALUES statements (about 80x faster than executemany in DuckDB: ~26k profile
    rows in about a second instead of minutes). The rows' primary keys must be unique (match() gives one row per
    security_id, and source_id is fixed), since one statement cannot replace the same row twice."""
    one = "(" + ",".join("?" * len(cols)) + ")"
    for i in range(0, len(rows), BULK_ROWS):
        part = rows[i:i + BULK_ROWS]
        con.execute(f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES " + ",".join([one] * len(part)),
                    [x for r in part for x in r])
    return len(rows)


def import_bz2(cfg: Config, con: duckdb.DuckDBPyConnection, fd_csv: str | Path, *, only_universe: bool = True
               ) -> dict[str, Any]:
    """import_descriptions(fd_csv=...): the bulk file path (see the module docstring)."""
    from .. import consent
    consent.require_gray_sources(cfg, SOURCE_ID)
    path = Path(fd_csv)
    started = store.now_utc()
    raw = path.read_bytes()
    fd_rows, skipped, _ = load_fd_csv_bz2(path)
    secs = _load_securities(con, only_universe)
    report: dict[str, Any] = {}
    matches = match(fd_rows, secs, report=report)
    con.begin()
    try:
        snapshot_id = _write_bz2(con, path, only_universe, store.sha256(raw), len(raw), matches, report, started)
        con.commit()
    except BaseException:
        con.rollback()
        raise
    by_method: dict[str, int] = {}
    for m in matches:
        by_method[m.method] = by_method.get(m.method, 0) + 1
    universe_total, universe_with = con.execute(
        """SELECT count(DISTINCT u.company_key), count(DISTINCT d.company_key)
           FROM universe u LEFT JOIN descriptions d ON d.company_key = u.company_key AND d.source_id = ?""",
        [SOURCE_ID]).fetchone()
    return {"snapshot_id": snapshot_id, "kind": BZ2_KIND, "fd_rows_total": skipped["fd_rows_total"],
            "fd_rows_considered": len(fd_rows), "skipped": {k: v for k, v in skipped.items() if k != "fd_rows_total"},
            "securities_considered": len(secs), "descriptions_written": len(matches),
            "matched_by_method": dict(sorted(by_method.items())), "conflicts": len(report["conflicts"]),
            "text_conflicts": len(report["text_conflicts"]), "stale_removed": report["stale_removed"],
            "universe_companies": int(universe_total), "universe_companies_with_description": int(universe_with),
            "seconds": round((store.now_utc() - started).total_seconds(), 2)}
