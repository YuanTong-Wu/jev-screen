"""Company name resolution for `jevscreen why` and the sieve author commands (free, local, read-only).

alias_index(con) collects every name a user may type for a listed company: the securities table (security_id, symbol,
TradingView name) and the official lists already in the store, located through `snapshots` (pack-imported kinds
too) and joined to lines through `identifiers` (never by re-mapping exchange codes):
- CNINFO stock list (cninfo_orgid): the A-share short name 简称, and its form without ST / *ST;
- EDINET code list (edinet_code): the Japanese filer name and its English name;
- DART corp codes (dart_corp_code): the Korean and English names of listed corps;
- SEC tickers (sec_cik): the SEC's company name and ticker.
A list that is missing or unreadable is skipped (`sources_used` says which were read).

resolve(index, text) scores candidates (see RESOLVE_RULES) and returns one Match: exact | fuzzy | ambiguous |
not_found. Sieve writes accept only score 1.0 (exact); `why` may use a fuzzy match and says so.
"""
from __future__ import annotations

import dataclasses
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable

UNIQUE_MIN, UNIQUE_GAP, JACCARD_MIN, CANDIDATES_MAX = 0.8, 0.15, 0.5, 5
RESOLVE_RULES = ("id / company_key / symbol / exact alias = 1.0; containment (alias >= 3 CJK characters or an "
                 "ST-stripped short name) = len(shorter) / len(longer); character-bigram Jaccard >= 0.5 = its value. "
                 "Unique when the best >= 0.8 and the next company <= best - 0.15; ties prefer companies in the run.")

_SUFFIX_EN = re.compile(r"(?:[\s,.]+(?:inc|incorporated|corp|corporation|co|company|ltd|limited|plc|ag|sa|nv|se|llc|"
                        r"lp|spa|s\.?p\.?a|oyj|asa|ab|bhd|tbk|pcl|holdings?|group|k\.?k|s\.?a|n\.?v)\.?)+$", re.I)
_CLASS = re.compile(r"\s+(?:class|cl\.?|series)\s+[a-z0-9]\.?$|\s+(?:ordinary shares|common stock|adr|ads)$", re.I)
_LEGAL_CJK = re.compile(r"股份有限公司$|有限责任公司$|有限公司$|^株式会社|株式会社$|[（(]株[)）]|^주식회사|주식회사$|"
                        r"[（(]주[)）]|㈜")
_PUNCT = re.compile(r"[\s\-_.,:;'\"“”‘’`!?()（）\[\]{}·・/&＆@#*+=|\\]+")
_CJK = re.compile(r"[㐀-䶿一-鿿぀-ヿ가-힯]")


def normalize(text: str | None) -> str:
    """NFKC, case-folded, share class and legal suffixes (Inc / Corp / 股份有限公司 / 株式会社 / 주식회사 …) removed,
    punctuation and spaces removed."""
    n = unicodedata.normalize("NFKC", text or "").strip()
    prev = None
    while prev != n:
        prev = n
        n = _CLASS.sub("", n).strip()
        n = _SUFFIX_EN.sub("", n).strip(" ,.")
        n = _LEGAL_CJK.sub("", n).strip()
    return _PUNCT.sub("", n).casefold()


def _bigrams(s: str) -> frozenset[str]:
    return frozenset(s[i:i + 2] for i in range(len(s) - 1)) or frozenset({s} if s else ())


@dataclasses.dataclass(frozen=True)
class Alias:
    text: str            # as the source spells it
    norm: str
    security_id: str
    company_key: str | None
    source: str          # securities | cninfo | edinet | dart | sec
    st_stripped: bool = False


@dataclasses.dataclass
class Match:
    status: str                      # exact | fuzzy | ambiguous | not_found
    query: str
    security_id: str | None = None   # the company's universe line when it has one, else the matched line
    company_key: str | None = None
    name: str | None = None
    score: float = 0.0
    method: str | None = None        # id | company_key | symbol | alias:<source> | contains:<source> | fuzzy:<source>
    matched_line: str | None = None  # the line the text named (an ADR, a B share ...) when it is not the universe line
    in_universe: bool = False
    candidates: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class Index:
    lines: dict[str, dict[str, Any]]          # security_id -> {company_key, name, symbol, exchange, active}
    universe: dict[str, str]                  # company_key -> universe security_id
    by_id: dict[str, str]                     # casefolded security_id -> security_id
    by_symbol: dict[str, list[str]]           # casefolded symbol -> [security_id]
    aliases: list[Alias]
    by_norm: dict[str, list[Alias]]
    sources_used: list[str]
    _grams: list[frozenset[str]] | None = None

    def grams(self) -> list[frozenset[str]]:
        if self._grams is None:
            self._grams = [_bigrams(a.norm) for a in self.aliases]
        return self._grams


def _newest_raw(con, source_id: str, kinds: Iterable[str]) -> str | None:
    kinds = list(kinds) + ["pack:" + k for k in kinds]
    ph = ",".join("?" for _ in kinds)
    for (path,) in con.execute(
            f"SELECT raw_path FROM snapshots WHERE source_id = ? AND kind IN ({ph}) AND raw_path IS NOT NULL "
            f"AND coalesce(status, 'ok') = 'ok' ORDER BY fetched_at DESC", [source_id, *kinds]).fetchall():
        if Path(path).exists():
            return path
    return None


def _jsonl_gz(path: str) -> list[dict]:
    import gzip
    return [json.loads(x) for x in gzip.decompress(Path(path).read_bytes()).decode("utf-8").splitlines() if x.strip()]


def _read_list(source: str, path: str) -> list[tuple[str, list[str], bool]]:
    """[(native id, [names], listed)] of one official list file."""
    if source == "cninfo":
        from . import shells
        return [(r["org_id"], [r["name"]] if r.get("name") else [], True) for r in shells.read_stock_list(path)]
    if source == "edinet":
        from .sources import edinet
        rows = _jsonl_gz(path) if path.endswith(".gz") else edinet.parse_codelist(Path(path).read_bytes())
        return [(r.get("edinet_code"), [n for n in (r.get("name"), r.get("name_en")) if n], r.get("listed") is not False)
                for r in rows if r.get("edinet_code")]
    if source == "dart":
        from .sources import dart
        rows = _jsonl_gz(path) if path.endswith(".gz") else dart.parse_corp_codes(Path(path).read_bytes())
        return [(r.get("corp_code"), [n for n in (r.get("corp_name"), r.get("corp_eng_name")) if n],
                 bool(r.get("stock_code"))) for r in rows if r.get("corp_code") and r.get("stock_code")]
    if source == "sec":
        from .sources import sec_edgar
        data = _jsonl_gz(path) if path.endswith(".gz") else Path(path).read_bytes()
        return [(str(r["cik"]), [r["name"]] if r.get("name") else [], True) for r in sec_edgar.parse_tickers(data)]
    return []


# source -> (snapshots.source_id, kinds, identifiers.id_type)
LISTS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "cninfo": ("cninfo_annual_report", ("stock_list",), "cninfo_orgid"),
    "edinet": ("edinet_yuho", ("edinet_codelist", "edinet_codes"), "edinet_code"),
    "dart": ("dart_business_report", ("dart_corpcode",), "dart_corp_code"),
    "sec": ("sec_tickers", ("company_tickers_exchange", "sec_tickers"), "sec_cik"),
}


def alias_index(con, *, lists: Iterable[str] = tuple(LISTS)) -> Index:
    """The alias index of the store (one read-only connection, no network)."""
    lines: dict[str, dict[str, Any]] = {}
    by_id: dict[str, str] = {}
    by_symbol: dict[str, list[str]] = {}
    aliases: list[Alias] = []
    for sid, ck, name, sym, exch, active in con.execute(
            "SELECT security_id, company_key, name, symbol, exchange, active FROM securities").fetchall():
        lines[sid] = {"company_key": ck, "name": name, "symbol": sym, "exchange": exch, "active": bool(active)}
        by_id[sid.casefold()] = sid
        if sym:
            by_symbol.setdefault(sym.casefold(), []).append(sid)
        if name and active:
            aliases.append(Alias(name, normalize(name), sid, ck, "securities"))
    universe = dict(con.execute("SELECT company_key, security_id FROM universe").fetchall())
    used = ["securities"]
    for src in lists:
        source_id, kinds, id_type = LISTS[src]
        path = _newest_raw(con, source_id, kinds)
        if path is None:
            continue
        try:
            rows = _read_list(src, path)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        by_native: dict[str, list[str]] = {}
        for sid, val in con.execute("SELECT security_id, id_value FROM identifiers WHERE id_type = ?",
                                    [id_type]).fetchall():
            by_native.setdefault(str(val).lstrip("0") if src == "sec" else str(val), []).append(sid)
        used.append(src)
        for native, names, listed in rows:
            key = str(native).lstrip("0") if src == "sec" else str(native)
            for sid in by_native.get(key, []):
                ck = (lines.get(sid) or {}).get("company_key")
                for n in names:
                    aliases.append(Alias(n, normalize(n), sid, ck, src))
                    if src == "cninfo":
                        from . import shells
                        bare = shells.strip_st(n)
                        if bare and bare != unicodedata.normalize("NFKC", n).strip():
                            aliases.append(Alias(bare, normalize(bare), sid, ck, src, st_stripped=True))
    by_norm: dict[str, list[Alias]] = {}
    for a in aliases:
        if a.norm:
            by_norm.setdefault(a.norm, []).append(a)
    return Index(lines=lines, universe=universe, by_id=by_id, by_symbol=by_symbol, aliases=aliases,
                 by_norm=by_norm, sources_used=used)


def _cjk_len(s: str) -> int:
    return len(_CJK.findall(s))


def _display(index: Index, sid: str) -> str | None:
    return (index.lines.get(sid) or {}).get("name")


def resolve(index: Index, text: str, *, prefer: Iterable[str] = ()) -> Match:
    """The company `text` names (a security_id, company_key, exchange code / ticker, or a name in any script).
    prefer: company_keys of the run (ties between companies go to one of them when exactly one is there)."""
    q = (text or "").strip()
    m = Match(status="not_found", query=q)
    if not q:
        return m
    prefer = set(prefer or ())
    scored: dict[str, tuple[float, str, str]] = {}      # company_key (or sid) -> (score, method, security_id)

    def add(sid: str, score: float, method: str) -> None:
        ck = (index.lines.get(sid) or {}).get("company_key") or sid
        cur = scored.get(ck)
        if cur is None or score > cur[0]:
            scored[ck] = (score, method, sid)

    qf = q.casefold()
    if qf in index.by_id:
        add(index.by_id[qf], 1.0, "id")
    for ck, usid in index.universe.items():
        if ck and ck.casefold() == qf:
            add(usid, 1.0, "company_key")
    sym = qf.rpartition(":")[2] if ":" in qf and qf not in index.by_id else qf
    for sid in index.by_symbol.get(sym, []):
        if index.lines[sid]["active"]:
            add(sid, 1.0, "symbol")
    nq = normalize(q)
    if nq:
        for a in index.by_norm.get(nq, []):
            add(a.security_id, 1.0, f"alias:{a.source}")
        if not scored:
            gq = _bigrams(nq)
            for a, g in zip(index.aliases, index.grams()):
                if not a.norm:
                    continue
                short, long_ = (a.norm, nq) if len(a.norm) <= len(nq) else (nq, a.norm)
                if (a.st_stripped or _cjk_len(a.norm) >= 3) and short in long_ and len(short) >= 2:
                    add(a.security_id, round(len(short) / len(long_), 4), f"contains:{a.source}")
                    continue
                if g and gq:
                    j = len(g & gq) / len(g | gq)
                    if j >= JACCARD_MIN:
                        add(a.security_id, round(j, 4), f"fuzzy:{a.source}")
    if not scored:
        return m
    ranked = sorted(scored.items(), key=lambda kv: (-kv[1][0], kv[0] not in prefer, kv[0]))
    cands = []
    for ck, (score, method, sid) in ranked[:CANDIDATES_MAX]:
        usid = index.universe.get(ck)
        cands.append({"security_id": usid or sid, "company_key": ck, "name": _display(index, usid or sid),
                      "score": score, "method": method})
    top_ck, (top, method, sid) = ranked[0]
    tied = [ck for ck, v in ranked if v[0] >= top - 1e-9]
    if len(tied) > 1:
        pick = [ck for ck in tied if ck in prefer]
        if len(pick) == 1:
            top_ck = pick[0]
            top, method, sid = scored[top_ck]
            rest = [v[0] for ck, v in ranked if ck not in tied]     # the run's line settles the tie
        else:
            return dataclasses.replace(m, status="ambiguous", score=top, candidates=cands)
    else:
        rest = [v[0] for _ck, v in ranked[1:]]
    second = rest[0] if rest else 0.0
    if top < UNIQUE_MIN or (second > top - UNIQUE_GAP and not (top >= 1.0 > second)):
        return dataclasses.replace(m, status="ambiguous" if len(cands) > 1 else "not_found", score=top,
                                   candidates=cands)
    usid = index.universe.get(top_ck)
    line = usid or sid
    return Match(status="exact" if top >= 1.0 else "fuzzy", query=q, security_id=line,
                 company_key=(index.lines.get(sid) or {}).get("company_key") or top_ck,
                 name=_display(index, line), score=top, method=method,
                 matched_line=sid if (usid and sid != usid) else None, in_universe=usid is not None,
                 candidates=cands)
