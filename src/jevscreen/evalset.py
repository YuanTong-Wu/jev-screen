"""The open evaluation set: labelled ideas, and the scores of a screen against them (`jevscreen eval`).

An eval file (evals/ideas/<id>.json, format jevscreen.eval/1) holds one idea and labels for the companies a screen
may surface:

    {"format": "jevscreen.eval/1", "id": "storage-liquid-cooling", "idea": "...", "idea_en": "...",
     "type": "product_category", "markets": ["CN"], "min_mcap_usd": 1e9, "countries": null,
     "labels": [{"security_id": "SZSE:301018", "name": "Shenling Environmental", "label": "right",
                 "must_include": true, "source_url": "https://...", "note": "own words, never a quote of a profile",
                 "by": "ai", "reviewed": false}]}

- label: right (an official filing or issuer IR document states the business the idea names), edge (related but broad, early or small),
  wrong (another role or no such business). must_include: the list is incomplete without it (recall).
- source_url: an official filing (SEC, CNINFO, EDINET, DART, MOPS, BSE, the exchange or the company's own investor
  page); never TradingView / Yahoo text, which is gray-private and not quoted here. note: the labeller's own words.
- by: ai | ai-adjudicated | human; reviewed: whether the maintainer checked the label. Reports count the unreviewed
  labels. ai-adjudicated: two blind AI labellers, disagreements decided by a third AI reading the filing.
  reviewed_by (optional): owner, only with reviewed: true (the owner decided this label himself).
  diversified (optional, right / edge): the filing names the idea's product line although it is a small part of a
  broader company (owner policy 2026-09-28, docs/EVAL.md); reports show how many top-10 rights are diversified.
  evidence_at (optional): where in the cited filing the statement is (page / section; no long quote).
  unresolved (optional, only with checked: search_summary): no official source established the label yet.
- Metrics: strict P@k (right / labelled; the headline) and lenient P@k ((right + edge) / labelled). A list shorter
  than k is scored over the n rows it shows (P@min(k, n)), n reported beside it; must-include recall is separate.
  checked (optional): filing_read (the filing/annual report was read), official_ir_read (an official issuer IR
  release or presentation was read), or search_summary (only a search engine summary was available). Reports count
  the weaker source classes separately; none of these means the owner reviewed the label.
- idea_en: required when the idea is not English (screen.needs_translation), so every machine asks Jev the same
  question (eval run never uses the local translation model).
- security_id: TradingView's EXCHANGE:SYMBOL, as the store has it. A few venue names are read as the store's code
  (KOSDAQ / KOSPI: KRX, HKG: HKEX, TYO: TSE) and an HKEX symbol without its leading zeros (HKEX:0700 is HKEX:700).
  When the store is at hand, a label also matches another listing line of the same company (company_key: the other
  line of a dual listing), and labels the store does not know are listed (not_in_store).

Scoring is free and local: it reads a run's results.json (rows in rank order) and never calls Jev. Companies in the
top k without a label are counted apart (`unlabelled`): precision is right / labelled, with the coverage beside it,
so an unlabelled company never counts as right or wrong. Only runs that finished (status ok / partial) enter the
means; the others (budget ran out, Jev busy or unavailable, an error, not run) are listed apart. Run-to-run noise:
Jev answers are cached, so a rerun of the same run is identical; the band reads (--reads) and --read-offset give
fresh reads (see docs/EVAL.md).
No network, no duckdb at import time.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

FORMAT = "jevscreen.eval/1"
BY = ("ai", "ai-adjudicated", "human")    # who labelled; ai-adjudicated: two blind AI labellers + an arbiter
LABELS = ("right", "edge", "wrong")
TYPES = ("product_category", "supply_chain", "geography", "customer_segment", "technology", "other")
KS = (10, 40)
OFFICIAL_HOSTS = (
    "sec.gov", "cninfo.com.cn", "sse.com.cn", "szse.cn", "bse.cn", "hkexnews.hk", "disclosure2.edinet-fsa.go.jp",
    "disclosure.edinet-fsa.go.jp", "dart.fss.or.kr", "mops.twse.com.tw", "doc.twse.com.tw", "bseindia.com",
    "nseindia.com", "jpx.co.jp", "krx.co.kr", "sgx.com", "idx.co.id", "set.or.th", "londonstockexchange.com",
    "asx.com.au")
GRAY_HOSTS = ("tradingview.com", "yahoo.com", "finance.yahoo", "financedatabase")
# mirrors, portals and news sites: not the filing itself (cite the exchange / regulator URL of the same document)
THIRD_PARTY_HOSTS = ("sina.com.cn", "sina.cn", "eastmoney.com", "xueqiu.com", "10jqka.com.cn", "fxbaogao.com",
                     "stcn.com", "cs.com.cn", "cnstock.com", "weeklyonstock.com", "hexun.com", "163.com", "qq.com",
                     "sohu.com", "baidu.com", "wikipedia.org", "bloomberg.com", "reuters.com", "marketscreener.com",
                     "stockanalysis.com", "macrotrends.net", "seekingalpha.com", "investing.com", "morningstar.com",
                     "wsj.com", "ft.com", "cnbc.com", "zhihu.com", "kabutan.jp", "minkabu.jp", "naver.com")
CHECKED = ("filing_read", "official_ir_read", "search_summary")
REVIEWERS = ("owner",)              # reviewed_by: who reviewed a label (reviewed: true)
SCORED = ("ok", "partial")          # run statuses whose scores enter the means (the others are listed apart)
# venue names people write -> the TradingView exchange code the store's security_id carries
VENUE_ALIASES = {"KOSDAQ": "KRX", "KOSPI": "KRX", "HKG": "HKEX", "TYO": "TSE"}
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
_SID = re.compile(r"^[A-Z][A-Z0-9_]*:\S+$")


class EvalError(ValueError):
    pass


def _host(url: str) -> str:
    m = re.match(r"^https?://([^/?#]+)", url or "", re.IGNORECASE)
    return (m.group(1).lower() if m else "").split("@")[-1].split(":")[0]


def source_kind(url: str | None) -> str:
    """official (an exchange or regulator filing site), gray (TradingView / Yahoo: never allowed), third_party (a
    mirror, portal or news site: not allowed), company (any other page, e.g. the company's own investor relations
    site) or missing."""
    if not url:
        return "missing"
    h = _host(url)
    if not h:
        return "missing"
    if any(h == g or h.endswith("." + g) or g in h for g in GRAY_HOSTS):
        return "gray"
    if any(h == o or h.endswith("." + o) for o in OFFICIAL_HOSTS):
        return "official"
    if any(h == t or h.endswith("." + t) for t in THIRD_PARTY_HOSTS):
        return "third_party"
    return "company"


def check(data: Mapping[str, Any]) -> list[str]:
    """Problems of one eval file (empty: ok)."""
    out: list[str] = []
    if data.get("format") != FORMAT:
        out.append(f"format is not {FORMAT}")
    if not _ID.match(str(data.get("id") or "")):
        out.append("id must be lower-case letters, digits and dashes")
    if not str(data.get("idea") or "").strip():
        out.append("idea is empty")
    if data.get("type") not in TYPES:
        out.append(f"type must be one of {', '.join(TYPES)}")
    idea_en = data.get("idea_en")
    if idea_en is not None and not isinstance(idea_en, str):
        out.append("idea_en must be a string")
    elif not str(idea_en or "").strip() and _needs_translation(str(data.get("idea") or "")):
        out.append("idea_en is empty: a non-English idea needs its fixed English sentence (the Jev questions use it)")
    m = data.get("min_mcap_usd")
    if m is not None and (isinstance(m, bool) or not isinstance(m, (int, float)) or not math.isfinite(m) or m < 0):
        out.append("min_mcap_usd must be a number >= 0 (or null: $1B)")
    c = data.get("countries")
    if c is not None and (not isinstance(c, list) or not all(isinstance(x, str) and x.strip() for x in c)):
        out.append("countries must be null or a list of country codes / names")
    mk = data.get("markets")
    if mk is not None and (not isinstance(mk, list) or not all(isinstance(x, str) and x.strip() for x in mk)):
        out.append("markets must be null or a list of ISO-2 country codes")
    elif mk:
        from . import screen
        try:           # eval run screens with it when countries is null: a typo must not empty the universe
            screen.country_matcher(list(mk))
        except ValueError as e:
            out.append(f"markets: {e}")
    labels = data.get("labels")
    if not isinstance(labels, list) or not labels:
        out.append("labels is empty")
        return out
    seen: set[str] = set()
    for i, lab in enumerate(labels, 1):
        if not isinstance(lab, dict):
            out.append(f"label {i} (?): not an object")
            continue
        raw = str(lab.get("security_id") or "")
        where = f"label {i} ({raw or '?'})"
        sid = _norm_sid(raw)
        if raw != raw.strip():
            out.append(f"{where}: security_id has spaces around it")
        if not _SID.match(sid):
            out.append(f"{where}: security_id must be EXCHANGE:SYMBOL")
        if sid in seen:        # compared as score() matches them (upper case, venue aliases)
            out.append(f"{where}: listed twice")
        seen.add(sid)
        if lab.get("label") not in LABELS:
            out.append(f"{where}: label must be right, edge or wrong")
        kind = source_kind(lab.get("source_url"))
        if kind == "gray":
            out.append(f"{where}: source_url must not be TradingView / Yahoo (gray-private)")
        elif kind == "third_party":
            out.append(f"{where}: source_url is a mirror / portal / news site (cite the official filing's URL)")
        elif kind == "missing":
            out.append(f"{where}: source_url is missing (an official filing)")
        if lab.get("checked") is not None and lab.get("checked") not in CHECKED:
            out.append(f"{where}: checked must be filing_read, official_ir_read or search_summary")
        if lab.get("must_include") and lab.get("label") != "right":
            out.append(f"{where}: must_include only on a 'right' label")
        if lab.get("by") not in BY:
            out.append(f"{where}: by must be {', '.join(BY)}")
        if not isinstance(lab.get("reviewed"), bool):
            out.append(f"{where}: reviewed must be a boolean")
        if len(str(lab.get("note") or "")) > 300:
            out.append(f"{where}: note is longer than 300 characters (own words, never a quoted profile)")
        if "diversified" in lab:
            if not isinstance(lab["diversified"], bool):
                out.append(f"{where}: diversified must be a boolean")
            elif lab["diversified"] and lab.get("label") not in ("right", "edge"):
                out.append(f"{where}: diversified only on a right or edge label")
        if "reviewed_by" in lab:
            if lab["reviewed_by"] not in REVIEWERS:
                out.append(f"{where}: reviewed_by must be {' or '.join(REVIEWERS)}")
            elif lab.get("reviewed") is not True:
                out.append(f"{where}: reviewed_by needs reviewed: true")
        if "evidence_at" in lab and (not isinstance(lab["evidence_at"], str) or len(lab["evidence_at"]) > 200):
            out.append(f"{where}: evidence_at must be text of at most 200 characters (the page / section, no quote)")
        if "unresolved" in lab:
            if not isinstance(lab["unresolved"], bool):
                out.append(f"{where}: unresolved must be a boolean")
            elif lab["unresolved"] and lab.get("checked") != "search_summary":
                out.append(f"{where}: unresolved only on a search_summary label")
    return out


def load(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise EvalError(f"{p}: {type(e).__name__}: {e}") from None
    if not isinstance(data, dict):
        raise EvalError(f"{p}: not a JSON object")
    probs = check(data)
    if probs:
        raise EvalError(f"{p}: " + "; ".join(probs))
    data["_path"] = str(p)
    return data


def load_dir(root: str | Path, ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Every eval file under root (evals/ideas/*.json), sorted by id; only `ids` when given (unknown id: error; a
    repeated id counts once). Two files with the same id: error."""
    files = sorted(Path(root).glob("*.json"))
    sets = [load(f) for f in files]
    first: dict[str, str] = {}
    for s in sets:
        if s["id"] in first:
            raise EvalError(f"eval idea {s['id']} is in two files: {first[s['id']]} and {s['_path']}")
        first[s["id"]] = s["_path"]
    want = list(dict.fromkeys(ids or []))
    if want:
        by = {s["id"]: s for s in sets}
        missing = [i for i in want if i not in by]
        if missing:
            raise EvalError(f"no eval idea {', '.join(missing)} under {root}")
        sets = [by[i] for i in want]
    return sorted(sets, key=lambda s: s["id"])


def _needs_translation(idea: str) -> bool:
    from .screen import needs_translation      # the screen's own test (Han / kana / Hangul, mostly non-Latin)
    return needs_translation(idea)


def _norm_sid(sid: str | None) -> str:
    """The id as the store writes it: upper case, a venue alias read as the store's code, HKEX without leading
    zeros."""
    s = str(sid or "").strip().upper()
    venue, sep, sym = s.partition(":")
    if not sep:
        return s
    venue = VENUE_ALIASES.get(venue, venue)
    if venue == "HKEX":
        sym = sym.lstrip("0") or "0"
    return f"{venue}:{sym}"


def _label_counts(data: Mapping[str, Any]) -> dict[str, int]:
    labs = [x for x in data.get("labels") or [] if isinstance(x, dict)]
    return {"labels": len(labs), "unreviewed_labels": sum(1 for x in labs if x.get("reviewed") is not True),
            "owner_reviewed_labels": sum(1 for x in labs if x.get("reviewed") is True
                                         and x.get("reviewed_by") == "owner"),
            "diversified_labels": sum(1 for x in labs if x.get("diversified") is True),
            "unresolved_labels": sum(1 for x in labs if x.get("unresolved") is True),
            "official_ir_labels": sum(1 for x in labs if x.get("checked") == "official_ir_read"),
            "search_summary_labels": sum(1 for x in labs if x.get("checked") == "search_summary")}


def screen_scope(data: Mapping[str, Any]) -> list[str] | None:
    """The country filter `eval run` screens an idea with: its `countries` when set, otherwise its `markets` (the
    markets the idea points at), otherwise None (the whole store). Matched as `screen --countries` matches (an ISO-2
    code is the country of incorporation). Runs before 2026-09-28 passed `countries` only, so most ideas screened the
    whole store."""
    for key in ("countries", "markets"):
        v = [str(x).strip() for x in data.get(key) or [] if str(x).strip()]
        if v:
            return v
    return None


def outside_scope(data: Mapping[str, Any], lines: Mapping[str, tuple[str | None, str | None]]) -> list[str]:
    """The right labels (must-include first) whose company the idea's screen filter can never show: lines
    ({label id: (country, exchange)} from the store) matched as `screen --countries` matches them. Labels the store
    does not know are not listed here (they are in not_in_store)."""
    scope = screen_scope(data)
    if not scope:
        return []
    from . import screen
    try:
        match = screen.country_matcher(scope, {c for c, _e in lines.values() if c})
    except ValueError:
        return []
    labs = [x for x in data.get("labels") or [] if isinstance(x, dict) and x.get("label") == "right"]
    labs.sort(key=lambda x: x.get("must_include") is not True)
    return [sid for sid in (_norm_sid(x.get("security_id")) for x in labs)
            if sid in lines and not match(*lines[sid])]


def unscored(data: Mapping[str, Any], status: str, *, run_id: str | None = None, cost_usd: float | None = None,
             error: str | None = None) -> dict[str, Any]:
    """An idea with nothing to score (Jev busy or unavailable, an error, not run): listed apart, never in a mean."""
    return {"id": data.get("id"), "type": data.get("type"), "run_id": run_id, "status": status,
            "cost_usd": cost_usd, "error": error, **_label_counts(data)}


def counted(s: Mapping[str, Any]) -> bool:
    """Whether a score enters the means: a scored run that finished (ok / partial)."""
    return "at" in s and s.get("status") in SCORED


LEVERS = (("l2_constraints", "l2-constraints"), ("shortlist", "shortlist"), ("lang_terms", "lang-terms"),
          ("second_search", "second-search"))


def levers_of(result: Mapping[str, Any]) -> list[str]:
    """The accuracy levers a run was made (or, for a --shortlist re-score, viewed) with: [] for a baseline run."""
    params = result.get("params") or {}
    out = [name for key, name in LEVERS if params.get(key)]
    if result.get("shortlist") and "shortlist" not in out:
        out.append("shortlist")
    if params.get("judge"):                   # screen --judge: the arm is part of the lever's name
        # a layer that was skipped (budget, Jev unavailable) left the old order: counted apart, never as the arm
        applied = (result.get("judge") or {}).get("applied")
        out.append(f"judge-{params['judge']}" + ("" if applied else " skipped"))
    return out


# evidence tiers of a listed row (structural plan §1: measured precision differs by tier, so an unlabelled row is
# credited at its own tier's precision in the estimated P@10)
TIERS = ("explicit_core", "explicit_adjacent", "profile_capped", "partial_core", "partial_adjacent", "other")


def evidence_tier(row: Mapping[str, Any]) -> str:
    """explicit_core / explicit_adjacent (L2 explicit; L1 core or not), profile_capped (a profile-only row whose L2
    read was explicit, capped at related), partial_core / partial_adjacent, other (e.g. a pinned row L2 did not
    verify)."""
    from . import shortlist
    core = "core" if row.get("l1_label") == "core" else "adjacent"
    lab = row.get("l2_label")
    if lab == "explicit":
        return f"explicit_{core}"
    if shortlist.capped_explicit(dict(row)):
        return "profile_capped"
    if lab == "partial":
        return f"partial_{core}"
    return "other"


def score(data: Mapping[str, Any], result: Mapping[str, Any], ks: Iterable[int] = KS, *,
          keys: Mapping[str, str] | None = None,
          lines: Mapping[str, tuple[str | None, str | None]] | None = None) -> dict[str, Any]:
    """One run against one eval idea: per k the right / edge / wrong / unlabelled counts of the top k, precision
    (right / labelled; lenient: (right + edge) / labelled) and coverage (labelled / shown); must-include recall in
    the whole list and in the top 10; the missing must-includes; cost and time from the result.
    keys ({label id: company_key}, from the store): a row also matches a label of another listing line of its
    company (matched_by_company), and the labels the store does not know are listed (not_in_store).
    lines ({label id: (country, exchange)}, from the store): the right labels the screen's country filter can never
    show are listed (outside_scope). screen_countries: the filter the run was made with (None: the whole store)."""
    from . import page, shortlist
    if result.get("shortlist"):
        # the sections as the page shows them: a row its page marks borderline is to confirm (shortlist.settle)
        result = shortlist.view(dict(result))
    labels = {_norm_sid(x["security_id"]): x for x in data.get("labels") or []}
    by_key: dict[str, str] = {}
    for sid in labels:
        if (keys or {}).get(sid):
            by_key.setdefault(keys[sid], sid)
    rows = sorted((r for r in result.get("rows") or [] if r.get("rank") is not None), key=lambda r: r["rank"])
    ids = [_norm_sid(r.get("security_id")) for r in rows]
    hits = [sid if sid in labels else by_key.get(r.get("company_key") or "") for sid, r in zip(ids, rows)]
    out: dict[str, Any] = {"id": data.get("id"), "type": data.get("type"), "run_id": result.get("run_id"),
                           "status": result.get("status"), "shown": len(ids), "at": {}}
    for k in ks:
        c = {lab: 0 for lab in LABELS}
        unl = []
        unl_tiers: dict[str, int] = {}
        div = 0
        for r, sid, hit in zip(rows[:k], ids[:k], hits[:k]):
            if hit is None:
                unl.append(sid)
                t = evidence_tier(r)
                unl_tiers[t] = unl_tiers.get(t, 0) + 1
            else:
                c[labels[hit]["label"]] += 1
                div += labels[hit]["label"] == "right" and labels[hit].get("diversified") is True
        n_lab, shown = sum(c.values()), len(ids[:k])
        # a list shorter than k is scored over the n rows it shows: P@min(k, n), with n reported beside it
        out["at"][str(k)] = {**c, "unlabelled": len(unl), "unlabelled_ids": unl, "unlabelled_tiers": unl_tiers,
                             "shown": shown, "n": shown,
                             "diversified_right": div,
                             "precision": round(c["right"] / n_lab, 4) if n_lab else None,
                             "precision_lenient": round((c["right"] + c["edge"]) / n_lab, 4) if n_lab else None,
                             "coverage": round(n_lab / shown, 4) if shown else None}
    # every listed row by evidence tier (labelled or not): the aggregate measures each tier's precision from these
    tiers: dict[str, dict[str, int]] = {}
    for r, hit in zip(rows, hits):
        t = tiers.setdefault(evidence_tier(r), {"right": 0, "edge": 0, "wrong": 0, "unlabelled": 0})
        t["unlabelled" if hit is None else labels[hit]["label"]] += 1
    out["tiers"] = tiers
    must = [s for s, x in labels.items() if x.get("must_include")]
    got = set(hits) - {None}
    top10 = set(hits[:10]) - {None}
    out["must_include"] = {"n": len(must), "found": sum(1 for s in must if s in got),
                           "found_top10": sum(1 for s in must if s in top10),
                           "missing": [s for s in must if s not in got]}
    mi = out["must_include"]
    mi["recall"] = round(mi["found"] / mi["n"], 4) if mi["n"] else None
    mi["recall_top10"] = round(mi["found_top10"] / mi["n"], 4) if mi["n"] else None
    # the main list (the headline since the owner decision of 2026-09-29: the list is never padded to 10): the rows
    # of the confirmed tier (their shortlist_tier, else the tier they would get: shortlist.in_main), its first ten,
    # P@min(10, n_main) with n_main shown, and must-include recall in the whole main list beside it
    if not any(r.get("shortlist_tier") for r in rows):
        # a single list: the tier each row would get, a row its page would mark borderline to confirm (copies)
        check = page.gap_check(dict(result))
        in_main = [shortlist.in_main({**r, "shown_gap": check(r)}) for r in rows]
    else:
        in_main = [shortlist.in_main(r) for r in rows]
    main_all = [(r, hit) for r, hit, m in zip(rows, hits, in_main) if m]
    c = {lab: 0 for lab in LABELS}
    unl = 0
    for _r, hit in main_all[:10]:
        if hit is None:
            unl += 1
        else:
            c[labels[hit]["label"]] += 1
    n_lab = sum(c.values())
    main_hits = {hit for _r, hit in main_all} - {None}
    m_found = sum(1 for s_ in must if s_ in main_hits)
    out["main"] = {"n": len(main_all[:10]), **c, "unlabelled": unl,
                   "precision": round(c["right"] / n_lab, 4) if n_lab else None,
                   "precision_lenient": round((c["right"] + c["edge"]) / n_lab, 4) if n_lab else None,
                   "total": len(main_all), "to_confirm": len(rows) - len(main_all),
                   "must_found": m_found, "must_n": len(must),
                   "must_recall": round(m_found / len(must), 4) if must else None}
    if any(r.get("shortlist_tier") for r in rows):
        # the same numbers under their earlier name (screen --shortlist / shortlist.view: the rows carry tiers)
        out["shortlist"] = {**{k: v for k, v in out["main"].items() if k not in ("total", "to_confirm")},
                            "high_total": len(main_all)}
    out["levers"] = levers_of(result)
    out["cost_usd"] = result.get("cost_usd")
    out["seconds"] = (result.get("timing") or {}).get("total_s")
    out.update(_label_counts(data))
    out["matched_by_company"] = sorted({h for sid, h in zip(ids, hits) if h is not None and h != sid})
    if keys is not None:
        out["not_in_store"] = [sid for sid in labels if sid not in keys]
    params = result.get("params") or {}
    out["screen_countries"] = params.get("countries") if "countries" in params else screen_scope(data)
    if lines is not None:
        out["outside_scope"] = outside_scope({**data, "countries": out["screen_countries"], "markets": None}, lines)
    return out


def _levers_line(scores: list[Mapping[str, Any]]) -> str:
    """'l2-constraints (9 of 9 ideas), ...' for the report header ('' for a baseline run)."""
    scored = [s for s in scores if "at" in s]
    names = [n for _k, n in LEVERS]
    names += sorted({n for s in scored for n in (s.get("levers") or ()) if n not in names})
    got = {n: sum(1 for s in scored if n in (s.get("levers") or ())) for n in names}
    return ", ".join(f"{n} ({c} of {len(scored)} ideas)" for n, c in got.items() if c)


def _mean(xs: list[float | None]) -> float | None:
    v = [x for x in xs if x is not None]
    return round(sum(v) / len(v), 4) if v else None


def tier_precision(scores: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """{tier: {right, edge, wrong, n (labelled), unlabelled, precision, precision_lenient}} pooled over the listed rows
    (every rank) of the scored runs; '_all' pools every tier (the credit of a tier with no labelled row)."""
    pool: dict[str, dict[str, int]] = {t: {"right": 0, "edge": 0, "wrong": 0, "unlabelled": 0} for t in TIERS}
    for s in scores:
        for t, c in (s.get("tiers") or {}).items():
            p = pool.setdefault(t, {"right": 0, "edge": 0, "wrong": 0, "unlabelled": 0})
            for k in p:
                p[k] += int(c.get(k) or 0)
    pool["_all"] = {k: sum(p[k] for t, p in pool.items() if t != "_all") for k in ("right", "edge", "wrong",
                                                                                   "unlabelled")}
    out = {}
    for t, p in pool.items():
        n = p["right"] + p["edge"] + p["wrong"]
        out[t] = {**p, "n": n, "precision": round(p["right"] / n, 4) if n else None,
                  "precision_lenient": round((p["right"] + p["edge"]) / n, 4) if n else None}
    return out


def estimated(s: Mapping[str, Any], tp: Mapping[str, Mapping[str, Any]], k: int = 10) -> tuple[float | None,
                                                                                              float | None]:
    """(strict, lenient) estimated P@min(k, n) of one score: labelled rows as labelled, each unlabelled row credited
    at its evidence tier's measured precision (tier_precision; a tier with no labelled row: the pooled one)."""
    a = (s.get("at") or {}).get(str(k)) or {}
    shown = int(a.get("shown") or 0)
    if not shown:
        return None, None
    unl = dict(a.get("unlabelled_tiers") or {})
    if sum(unl.values()) != int(a.get("unlabelled") or 0):          # a score written before the tiers existed
        return None, None
    strict, lenient = float(a.get("right") or 0), float(a.get("right") or 0) + float(a.get("edge") or 0)
    for t, n in unl.items():
        p = tp.get(t) or {}
        if not p.get("n"):
            p = tp.get("_all") or {}
        if p.get("precision") is None:
            return None, None
        strict += n * p["precision"]
        lenient += n * p["precision_lenient"]
    return round(strict / shown, 4), round(lenient / shown, 4)


def aggregate(scores: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Means over the ideas whose run finished (counted: precision@k, lenient, coverage, must-include recall, the
    estimated P@10, the high-confidence tier's P@min(10, n)), overall and per idea type; tier_precision (pooled over
    the counted runs); the others in left_out (id, status); cost_usd_total: every idea's Jev cost."""
    done = [s for s in scores if counted(s)]
    tp = tier_precision(done)

    def agg(ss: list[Mapping[str, Any]]) -> dict[str, Any]:
        d: dict[str, Any] = {"ideas": len(ss)}
        for k in KS:
            d[f"p@{k}"] = _mean([s["at"].get(str(k), {}).get("precision") for s in ss])
            d[f"p_lenient@{k}"] = _mean([s["at"].get(str(k), {}).get("precision_lenient") for s in ss])
            d[f"coverage@{k}"] = _mean([s["at"].get(str(k), {}).get("coverage") for s in ss])
        est = [estimated(s, tp) for s in ss]
        d["p_est@10"] = _mean([e[0] for e in est])
        d["p_est_lenient@10"] = _mean([e[1] for e in est])
        mains = [s["main"] for s in ss if s.get("main")]
        if mains:
            # pooled over the main-list rows (the headline: the same kind of number as tier_precision's
            # explicit_core, right / labelled over every idea's first ten main rows) and the mean over ideas beside it
            # (an idea with one main row weighs as much as one with ten; an empty main list is left out)
            r_ = sum(int(x.get("right") or 0) for x in mains)
            e_ = sum(int(x.get("edge") or 0) for x in mains)
            lab_ = r_ + e_ + sum(int(x.get("wrong") or 0) for x in mains)
            d["p_main"] = round(r_ / lab_, 4) if lab_ else None
            d["p_main_lenient"] = round((r_ + e_) / lab_, 4) if lab_ else None
            d["n_main_labelled"] = lab_
            d["p_main_mean"] = _mean([x.get("precision") for x in mains])
            d["p_main_mean_lenient"] = _mean([x.get("precision_lenient") for x in mains])
            d["n_main"] = sum(int(x.get("n") or 0) for x in mains)
            d["main_total"] = sum(int(x.get("total") or 0) for x in mains)
            d["ideas_main_empty"] = sum(1 for x in mains if not x.get("n"))
            d["must_found_main"] = sum(int(x.get("must_found") or 0) for x in mains)
            d["must_recall_main"] = _mean([x.get("must_recall") for x in mains])
        hi = [s["shortlist"] for s in ss if s.get("shortlist")]
        if hi:
            d["p_high"] = _mean([x.get("precision") for x in hi])
            d["p_high_lenient"] = _mean([x.get("precision_lenient") for x in hi])
            d["n_high"] = sum(int(x.get("n") or 0) for x in hi)
            d["ideas_high_empty"] = sum(1 for x in hi if not x.get("n"))
        d["must_recall"] = _mean([s["must_include"]["recall"] for s in ss])
        d["must_recall_top10"] = _mean([s["must_include"]["recall_top10"] for s in ss])
        d["must_found_top10"] = sum(int(s["must_include"].get("found_top10") or 0) for s in ss)
        d["must_n"] = sum(int(s["must_include"].get("n") or 0) for s in ss)
        d["cost_usd"] = round(sum(float(s.get("cost_usd") or 0) for s in ss), 6)
        return d
    types = sorted({s.get("type") for s in done if s.get("type")})
    return {"all": agg(done), "by_type": {t: agg([s for s in done if s.get("type") == t]) for t in types},
            "tier_precision": tp,
            "left_out": [{"id": s.get("id"), "status": s.get("status")} for s in scores if not counted(s)],
            "cost_usd_total": round(sum(float(s.get("cost_usd") or 0) for s in scores), 6)}


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v:.0%}"


def _two_lists_lines(a: Mapping[str, Any], agg: Mapping[str, Any]) -> list[str]:
    """The two-list reading (structural plan step 1): the full list, the high-confidence tier, the estimated P@10
    and the evidence tiers' measured precision."""
    if a.get("p_est@10") is None and a.get("p_high") is None:
        return []
    out = ["", "Two lists: full list strict P@10 " + _pct(a.get("p@10")) + " (lenient " + _pct(a.get("p_lenient@10"))
           + "), estimated P@10 " + _pct(a.get("p_est@10")) + " (lenient " + _pct(a.get("p_est_lenient@10"))
           + "; each unlabelled row credited at its evidence tier's measured precision below)"
           + (f"; high-confidence tier strict P@min(10, n) {_pct(a.get('p_high'))} (lenient "
              f"{_pct(a.get('p_high_lenient'))}, {a.get('n_high', 0)} rows, {a.get('ideas_high_empty', 0)} ideas "
              "with an empty tier)" if a.get("p_high") is not None or a.get("n_high") is not None else "")
           + f"; must-include in the top 10: {a.get('must_found_top10', 0)}/{a.get('must_n', 0)}."]
    tp = agg.get("tier_precision") or {}
    rows = [(t, tp[t]) for t in (*TIERS, "_all") if t in tp and (tp[t]["n"] or tp[t]["unlabelled"])]
    if rows:
        out += ["", "| evidence tier | labelled | strict | lenient | r/e/w | unlabelled |", "|---|---|---|---|---|---|"]
        out += [f"| {'all tiers' if t == '_all' else t} | {p['n']} | {_pct(p['precision'])} | "
                f"{_pct(p['precision_lenient'])} | {p['right']}/{p['edge']}/{p['wrong']} | {p['unlabelled']} |"
                for t, p in rows]
    return out


def report_md(scores: list[Mapping[str, Any]], agg: Mapping[str, Any], *, title: str = "jev-screen eval") -> str:
    """A short Markdown report: the overall line, per type, per idea (with the run's status and its unreviewed
    labels), the ideas left out of the means, and the unlabelled companies to label next."""
    a = agg["all"]
    left = agg.get("left_out") or []
    n_lab = sum(int(s.get("labels") or 0) for s in scores)
    unrev = sum(int(s.get("unreviewed_labels") or 0) for s in scores)
    ir = sum(int(s.get("official_ir_labels") or 0) for s in scores)
    owner = sum(int(s.get("owner_reviewed_labels") or 0) for s in scores)
    unres = sum(int(s.get("unresolved_labels") or 0) for s in scores)
    summ = sum(int(s.get("search_summary_labels") or 0) for s in scores)
    cost = agg.get("cost_usd_total", a["cost_usd"])
    head = f"{a['ideas']} ideas scored" + (f", {len(left)} left out of the means" if left else "")
    provenance = (f"{unrev} of {n_lab} labels are unreviewed"
                  + (f" ({summ} checked only against a search summary)" if summ else "")
                  + ": numbers built on them are provisional." if unrev else "Every label is reviewed.")
    if owner:
        provenance += f" {owner} reviewed by the owner."
    if unres:
        provenance += f" {unres} unresolved (no official source established the label)."
    if summ and not unrev:
        provenance += f" {summ} checked only against a search summary: those labels still need primary evidence."
    if ir:
        provenance += f" {ir} checked against official IR material rather than a filing."
    lines = [f"# {title}", "",
             *([f"Main list (the confirmed tier, never padded to 10): strict P@min(10, n_main) pooled over the "
                f"main-list rows {_pct(a['p_main'])} (lenient {_pct(a.get('p_main_lenient'))}; "
                f"{a.get('n_main_labelled', 0)} labelled), mean over ideas {_pct(a.get('p_main_mean'))} (lenient "
                f"{_pct(a.get('p_main_mean_lenient'))}); n_main {a.get('n_main', 0)} rows over {a['ideas']} "
                f"ideas ({a.get('ideas_main_empty', 0)} with an empty main list), must-include in the main list "
                f"{a.get('must_found_main', 0)}/{a.get('must_n', 0)} (recall {_pct(a.get('must_recall_main'))}).", ""]
               if "n_main" in a else []),
             f"{head}. Full list: strict P@10 {_pct(a['p@10'])} (lenient {_pct(a['p_lenient@10'])}, labelled "
             f"{_pct(a['coverage@10'])}), strict P@40 {_pct(a['p@40'])} (lenient {_pct(a['p_lenient@40'])}, labelled "
             f"{_pct(a['coverage@40'])}), must-include recall {_pct(a['must_recall'])} (top 10: "
             f"{_pct(a['must_recall_top10'])}); Jev ${float(cost):.4f}.",
             *(["", f"Levers on: {lv}."] if (lv := _levers_line(scores)) else []),
             *_two_lists_lines(a, agg),
             "", "Strict precision of the main list is the headline: the rows of the confirmed tier (L1 core + L2 "
             "explicit, or judge tier A), which the product lists apart from the to-confirm section; the full list's "
             "numbers are the whole ranked list as before. Strict precision counts only labelled companies (right / "
             "labelled); lenient counts edge as right. A list shorter than k is scored over the n rows it shows: "
             "P@min(10, n), with n in the table. A diversified right is a company whose filing names the idea's product line although it is "
             "a small part of the company. " + provenance
             + (" Labels were matched by security_id only (the store was not read): the other line of a dual "
                "listing counts as unlabelled." if any("at" in s and "not_in_store" not in s for s in scores) else ""),
             "", "## By idea type", "",
             "| type | ideas | strict P@10 | lenient P@10 | strict P@40 | lenient P@40 | must-include recall |",
             "|---|---|---|---|---|---|---|"]
    for t, d in agg["by_type"].items():
        lines.append(f"| {t} | {d['ideas']} | {_pct(d['p@10'])} | {_pct(d['p_lenient@10'])} | {_pct(d['p@40'])} | "
                     f"{_pct(d['p_lenient@40'])} | {_pct(d['must_recall'])} |")
    lines += ["", "## By idea", "",
              "| idea | status | run | n@10 | strict P@10 (right/labelled; r/e/w/?) | lenient P@10 | diversified right "
              "in top 10 | n@40 | strict P@40 (right/labelled; r/e/w/?) | lenient P@40 | must-include | missing | "
              "unreviewed labels | cost |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in scores:
        cells = ["-"] * 9
        if "at" in s:
            for i, k in enumerate(KS):
                x = s["at"][str(k)]
                n_lab = x["right"] + x["edge"] + x["wrong"]
                strict = (f"{_pct(x['precision'])} ({x['right']}/{n_lab}; "
                          f"{x['right']}/{x['edge']}/{x['wrong']}/{x['unlabelled']})")
                cells[i * 4: i * 4 + 3] = [str(x.get("n", x["shown"])), strict, _pct(x["precision_lenient"])]
                if k == 10:
                    cells[3] = str(x.get("diversified_right", 0))
            cells = cells[:7]
            mi = s["must_include"]
            cells += [f"{mi['found']}/{mi['n']}", ", ".join(mi["missing"]) or "-"]
        ss = int(s.get("search_summary_labels") or 0)
        ir_s = int(s.get("official_ir_labels") or 0)
        kinds = ([f"{ss} search summary"] if ss else []) + ([f"{ir_s} official IR"] if ir_s else [])
        rev = f"{s.get('unreviewed_labels') or 0}/{s.get('labels') or 0}" + \
            (f" ({', '.join(kinds)})" if kinds else "")
        lines.append(f"| {s['id']} | {s.get('status') or '-'} | {s.get('run_id') or '-'} | {' | '.join(cells)} | "
                     f"{rev} | ${float(s.get('cost_usd') or 0):.4f} |")
    sl = [s for s in scores if s.get("main") or s.get("shortlist")]
    if sl:
        lines += ["", "## Main list (the confirmed tier: L1 core + L2 explicit; P@min(10, n_main), n_main shown)", "",
                  "| idea | n_main (of all main) | strict | lenient | r/e/w/? | must-include in the main list |",
                  "|---|---|---|---|---|---|"]
        for s in sl:
            x = s.get("main") or s["shortlist"]
            mi = f"{x['must_found']}/{x['must_n']}" if x.get("must_n") is not None else "-"
            lines.append(f"| {s['id']} | {x['n']} ({x.get('total', x.get('high_total', x['n']))}) | "
                         f"{_pct(x['precision'])} | {_pct(x['precision_lenient'])} | "
                         f"{x['right']}/{x['edge']}/{x['wrong']}/{x['unlabelled']} | {mi} |")
    if left:
        lines += ["", "## Left out of the means (the run did not finish)", ""]
        lines += [f"- {s['id']}: {s.get('status') or '-'}" + (f" ({s['error']})" if s.get("error") else "")
                  for s in scores if not counted(s)]
    scoped = [s for s in scores if "at" in s and "screen_countries" in s]
    if scoped:
        lines += ["", "## Screen filter (the idea's countries, else its markets; before 2026-09-28 countries only)",
                  ""]
        lines += [f"- {s['id']}: " + (", ".join(s["screen_countries"]) if s.get("screen_countries")
                                      else "the whole store") for s in scoped]
    outside = {s["id"]: s["outside_scope"] for s in scores if s.get("outside_scope")}
    if outside:
        lines += ["", "## Right labels outside the screen filter (the list can never show them: fix the idea's "
                  "markets, or the label)", "",
                  "A country code matches the country of incorporation (as `screen --countries` does): a company "
                  "listed in Hong Kong but incorporated in China needs CN.", ""]
        lines += [f"- {i}: {', '.join(v)}" for i, v in outside.items()]
    unknown = {s["id"]: s["not_in_store"] for s in scores if s.get("not_in_store")}
    if unknown:
        lines += ["", "## Label ids the store does not know (check the EXCHANGE:SYMBOL)", ""]
        lines += [f"- {i}: {', '.join(v)}" for i, v in unknown.items()]
    todo = {s["id"]: s["at"]["10"]["unlabelled_ids"] for s in scores if "at" in s and s["at"]["10"]["unlabelled_ids"]}
    if todo:
        lines += ["", "## Unlabelled in the top 10 (label these next)", ""]
        lines += [f"- {i}: {', '.join(v)}" for i, v in todo.items()]
    return "\n".join(lines) + "\n"
