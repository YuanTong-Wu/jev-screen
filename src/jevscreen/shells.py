"""Shells filter (排除壳公司) and exchange risk marks, applied by screen before layer 1. Free, local, explainable.

Rules (docs/SHELLS.md; SHELLS_VERSION bumps whenever a rule changes what it matches):
- `spac` (drop): the name looks like a blank-check vehicle (SPAC_NAME) or the first SPAC_TEXT_CHARS characters of a
  profile say the company *is* one (SPAC_TEXT: "is a blank check company", not "formerly a blank check company"),
  AND TTM revenue is below SPAC_MAX_REVENUE_USD or missing, AND the TradingView industry is one of SPAC_INDUSTRIES
  (or missing). A name / text hit outside those industries only gets the flag `spac_like` (explained by `why`, never
  dropped): an operating company that kept "Acquisition Corp" in its name after its merger is not a shell.
- `st` / `star_st` (flag, never dropped): the newest CNINFO stock list names the A share ST / *ST (S*ST counts as
  *ST): the exchange's risk warning. A missing list, or one older than ST_STALE_DAYS, is a gap, never a clean bill.
- Protected companies (every company named in the idea's sieve: card answers and pins, should_pass, should_fail) are
  never dropped: the filter must not cancel a human's answer (effective_drops).
- Evidence is stored as rule id + pattern id + character offsets, never the text itself (the run ledger is kept
  next to reports that may be shared).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable

SHELLS_VERSION = 1
MODES = ("drop", "keep")
SPAC_INDUSTRIES = frozenset({"Financial Conglomerates", "Finance/Rental/Leasing"})   # plus a missing industry
SPAC_MAX_REVENUE_USD = 1e6
SPAC_TEXT_CHARS = 300
ST_STALE_DAYS = 14
ST_SOURCE, ST_KIND, ST_ID_TYPE = "cninfo_annual_report", "stock_list", "cninfo_orgid"

# pattern id -> regex; the first pattern that matches is the evidence
SPAC_NAME: tuple[tuple[str, re.Pattern], ...] = (
    ("name_acq", re.compile(r"\bacquisitions? (?:corp(?:oration)?|company|co|ltd|limited|inc|holdings)\b", re.I)),
    ("name_spac", re.compile(r"\bSPAC\b")),
    ("name_merger", re.compile(r"\bmerger corp(?:oration)?\b", re.I)),
    ("name_special", re.compile(r"\bspecial purpose acquisition\b", re.I)),
)
SPAC_TEXT = re.compile(r"\b(?:is|was formed as|operates as) an? (?:newly (?:organized|incorporated) )?"
                       r"(?:blank[- ]check|special purpose acquisition)", re.I)
_ST = re.compile(r"^\s*(S?\s*\*\s*ST|ST)(?=\s*\S)", re.I)

MARK_ZH = {"st": "ST风险警示", "star_st": "*ST退市风险", "spac": "SPAC", "spac_like": "像SPAC的名字"}
MARK_EN = {"st": "ST risk warning", "star_st": "*ST delisting risk", "spac": "SPAC", "spac_like": "SPAC-like name"}
RULE_ZH = {"spac": "空壳收购公司（SPAC）"}
RULE_EN = {"spac": "blank-check company (SPAC)"}
ST_WARNING_ZH = "注意：交易所风险警示（ST）——公司财务或经营有问题；*ST 表示可能退市。排名不变，这是事实提示。"
ST_WARNING_EN = ("Note: exchange risk warning (ST): financial or operating trouble; *ST means possible delisting. "
                 "Rank unchanged.")


@dataclasses.dataclass
class StList:
    """The ST / *ST marks of the newest CNINFO stock list: {company_key: 'st' | 'star_st'}."""
    marks: dict[str, str]
    snapshot_id: str | None
    fetched_at: str | None
    stale: bool

    def meta(self) -> dict[str, Any]:
        return {"snapshot_id": self.snapshot_id, "fetched_at": self.fetched_at, "stale": self.stale}


def st_mark(short_name: str | None) -> str | None:
    """'star_st' for *ST / S*ST, 'st' for ST, else None (CNINFO zwjc, NFKC: full-width letters count)."""
    m = _ST.match(unicodedata.normalize("NFKC", short_name or ""))
    if not m:
        return None
    return "star_st" if "*" in m.group(1) else "st"


def strip_st(short_name: str | None) -> str:
    """The short name without its ST / *ST prefix ('*ST示信' -> '示信')."""
    n = unicodedata.normalize("NFKC", short_name or "").strip()
    m = _ST.match(n)
    return n[m.end():].strip() if m else n


def _newest_list(con) -> tuple[str, str, Any] | None:
    """(raw_path, snapshot_id, fetched_at) of the newest ok CNINFO stock list (also one imported by a pack) whose raw
    file exists."""
    rows = con.execute(
        "SELECT raw_path, snapshot_id, fetched_at FROM snapshots WHERE source_id = ? AND (kind = ? OR kind = ?) "
        "AND raw_path IS NOT NULL AND coalesce(status, 'ok') = 'ok' ORDER BY fetched_at DESC",
        [ST_SOURCE, ST_KIND, "pack:" + ST_KIND]).fetchall()
    for path, snap, at in rows:
        if Path(path).exists():
            return path, snap, at
    return None


def read_stock_list(path: str) -> list[dict]:
    """[{code, org_id, name}] of a raw CNINFO stock list file (json, or a pack's jsonl.gz)."""
    from .sources import cninfo
    data = Path(path).read_bytes()
    if path.endswith(".gz"):
        import gzip
        data = [json.loads(x) for x in gzip.decompress(data).decode("utf-8").splitlines() if x.strip()]
    return cninfo.parse_stock_list(data)


def st_list(con, today: dt.date | None = None) -> StList | None:
    """StList from the newest CNINFO stock list, joined to lines through identifiers.cninfo_orgid (never by
    re-mapping exchange codes). None when there is no list."""
    got = _newest_list(con)
    if got is None:
        return None
    path, snap, at = got
    try:
        rows = read_stock_list(path)
    except (OSError, ValueError):
        return None
    by_org = {}
    for r in rows:
        mk = st_mark(r.get("name"))
        if mk:
            by_org[r["org_id"]] = mk
    marks: dict[str, str] = {}
    if by_org:
        for ck, org in con.execute(
                "SELECT s.company_key, i.id_value FROM identifiers i JOIN securities s USING (security_id) "
                "WHERE i.id_type = ? AND s.company_key IS NOT NULL", [ST_ID_TYPE]).fetchall():
            if org in by_org:
                marks[ck] = by_org[org]
    today = today or dt.datetime.now(dt.timezone.utc).date()
    at_d = at.date() if isinstance(at, dt.datetime) else at
    stale = at_d is None or (today - at_d).days > ST_STALE_DAYS
    return StList(marks=marks, snapshot_id=snap, fetched_at=str(at) if at else None, stale=stale)


def load_facts(con, *, min_mcap_usd: float) -> dict[str, tuple[str | None, float | None]]:
    """security_id -> (industry, newest TTM total revenue in USD or None) of the universe lines at the floor."""
    rows = con.execute("""
        WITH u AS (SELECT security_id, industry FROM universe
                   WHERE market_cap_usd IS NOT NULL AND market_cap_usd >= ?),
             rev AS (SELECT f.security_id, f.value_usd FROM fundamentals_current f
                     JOIN (SELECT security_id, max(as_of) AS as_of FROM fundamentals_current
                           WHERE metric = 'total_revenue' AND period = 'ttm' GROUP BY 1) x
                       ON x.security_id = f.security_id AND x.as_of = f.as_of
                     WHERE f.metric = 'total_revenue' AND f.period = 'ttm')
        SELECT u.security_id, u.industry, rev.value_usd FROM u LEFT JOIN rev USING (security_id)""",
                       [min_mcap_usd]).fetchall()
    return {sid: (ind, rv) for sid, ind, rv in rows}


def _spac_evidence(name: str | None, texts: Iterable[str]) -> dict[str, Any] | None:
    for pid, pat in SPAC_NAME:
        m = pat.search(name or "")
        if m:
            return {"pat": pid, "in": "name", "off": [m.start(), m.end()]}
    for i, t in enumerate(texts):
        m = SPAC_TEXT.search((t or "")[:SPAC_TEXT_CHARS])
        if m:
            return {"pat": "text_blank_check", "in": f"profile{i}", "off": [m.start(), m.end()]}
    return None


def classify(universe: list[dict[str, Any]], descs: dict[str, list[tuple[str, str, bool]]] | None,
             facts: dict[str, tuple[str | None, float | None]] | None, st: StList | None
             ) -> dict[str, dict[str, Any]]:
    """{company_key: hit} for the universe rows (dicts with company_key, security_id, name) that a rule matched.
    hit = {'security_id', 'drop': ['spac'] (the rules that would drop it), 'flags': ['st' | 'star_st' | 'spac' | 'spac_like'],
    'ev': {'spac': {'pat', 'in', 'off'}}, 'industry', 'revenue_ttm_usd'}. Deterministic; descs are the raw
    (source_id, text, own) lists screen loads (sorted by source here, so the order in the store does not matter)."""
    out: dict[str, dict[str, Any]] = {}
    descs, facts = descs or {}, facts or {}
    for r in universe:
        ck = r["company_key"]
        hit: dict[str, Any] = {"security_id": r["security_id"], "drop": [], "flags": [], "ev": {}}
        texts = [t for _s, t, _o in sorted(descs.get(ck, []), key=lambda x: (x[0], not x[2]))]
        ev = _spac_evidence(r.get("name"), texts)
        if ev is not None:
            industry, revenue = facts.get(r["security_id"], (None, None))
            hit.update(industry=industry, revenue_ttm_usd=revenue)
            hit["ev"]["spac"] = ev
            if (industry is None or industry in SPAC_INDUSTRIES) and (revenue is None
                                                                      or revenue < SPAC_MAX_REVENUE_USD):
                hit["drop"].append("spac")
                hit["flags"].append("spac")
            else:
                hit["flags"].append("spac_like")
        mk = (st.marks.get(ck) if st is not None else None)
        if mk:
            hit["flags"].append(mk)
        if hit["drop"] or hit["flags"]:
            out[ck] = hit
    return out


def effective_drops(hits: dict[str, dict[str, Any]], mode: str, protected: Iterable[str] = ()) -> set[str]:
    """The company_keys the filter removes: every hit with a drop rule in mode 'drop', minus the protected
    companies (company_key or security_id of every company the sieve names). Mode 'keep' drops nothing."""
    if mode not in MODES:
        raise ValueError(f"shells must be one of {', '.join(MODES)} (got {mode!r})")
    if mode == "keep":
        return set()
    prot = set(protected or ())
    return {ck for ck, h in hits.items() if h["drop"] and ck not in prot and h.get("security_id") not in prot}


def top_st_rows(rows: list[dict[str, Any]], top_n: int = 20) -> list[dict[str, Any]]:
    """The output rows among the first top_n with an ST / *ST flag (console and report warning)."""
    return [r for r in rows[:top_n] if {"st", "star_st"} & set(r.get("flags") or ())]
