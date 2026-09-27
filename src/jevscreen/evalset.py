"""The open evaluation set: labelled ideas, and the scores of a screen against them (`jevscreen eval`).

An eval file (evals/ideas/<id>.json, format jevscreen.eval/1) holds one idea and labels for the companies a screen
may surface:

    {"format": "jevscreen.eval/1", "id": "storage-liquid-cooling", "idea": "...", "idea_en": "...",
     "type": "product_category", "markets": ["CN"], "min_mcap_usd": 1e9, "countries": null,
     "labels": [{"security_id": "SZSE:301018", "name": "Shenling Environmental", "label": "right",
                 "must_include": true, "source_url": "https://...", "note": "own words, never a quote of a profile",
                 "by": "ai", "reviewed": false}]}

- label: right (the official filing states the business the idea names), edge (related but broad, early or small),
  wrong (another role or no such business). must_include: the list is incomplete without it (recall).
- source_url: an official filing (SEC, CNINFO, EDINET, DART, MOPS, BSE, the exchange or the company's own investor
  page); never TradingView / Yahoo text, which is gray-private and not quoted here. note: the labeller's own words.
- by: ai | human; reviewed: whether the maintainer checked the label. Reports count the unreviewed labels.
  checked (optional): filing_read (the labeller read the filing) or search_summary (only a search engine's summary of
  that filing was available: weaker, always to be reviewed; reports count these too).
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
CHECKED = ("filing_read", "search_summary")
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
            out.append(f"{where}: checked must be filing_read or search_summary")
        if lab.get("must_include") and lab.get("label") != "right":
            out.append(f"{where}: must_include only on a 'right' label")
        if lab.get("by") not in ("ai", "human"):
            out.append(f"{where}: by must be ai or human")
        if len(str(lab.get("note") or "")) > 300:
            out.append(f"{where}: note is longer than 300 characters (own words, never a quoted profile)")
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
    return {"labels": len(labs), "unreviewed_labels": sum(1 for x in labs if not x.get("reviewed")),
            "search_summary_labels": sum(1 for x in labs if x.get("checked") == "search_summary")}


def unscored(data: Mapping[str, Any], status: str, *, run_id: str | None = None, cost_usd: float | None = None,
             error: str | None = None) -> dict[str, Any]:
    """An idea with nothing to score (Jev busy or unavailable, an error, not run): listed apart, never in a mean."""
    return {"id": data.get("id"), "type": data.get("type"), "run_id": run_id, "status": status,
            "cost_usd": cost_usd, "error": error, **_label_counts(data)}


def counted(s: Mapping[str, Any]) -> bool:
    """Whether a score enters the means: a scored run that finished (ok / partial)."""
    return "at" in s and s.get("status") in SCORED


def score(data: Mapping[str, Any], result: Mapping[str, Any], ks: Iterable[int] = KS, *,
          keys: Mapping[str, str] | None = None) -> dict[str, Any]:
    """One run against one eval idea: per k the right / edge / wrong / unlabelled counts of the top k, precision
    (right / labelled; lenient: (right + edge) / labelled) and coverage (labelled / shown); must-include recall in
    the whole list and in the top 10; the missing must-includes; cost and time from the result.
    keys ({label id: company_key}, from the store): a row also matches a label of another listing line of its
    company (matched_by_company), and the labels the store does not know are listed (not_in_store)."""
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
        for sid, hit in zip(ids[:k], hits[:k]):
            if hit is None:
                unl.append(sid)
            else:
                c[labels[hit]["label"]] += 1
        n_lab, shown = sum(c.values()), len(ids[:k])
        out["at"][str(k)] = {**c, "unlabelled": len(unl), "unlabelled_ids": unl, "shown": shown,
                             "precision": round(c["right"] / n_lab, 4) if n_lab else None,
                             "precision_lenient": round((c["right"] + c["edge"]) / n_lab, 4) if n_lab else None,
                             "coverage": round(n_lab / shown, 4) if shown else None}
    must = [s for s, x in labels.items() if x.get("must_include")]
    got = set(hits) - {None}
    top10 = set(hits[:10]) - {None}
    out["must_include"] = {"n": len(must), "found": sum(1 for s in must if s in got),
                           "found_top10": sum(1 for s in must if s in top10),
                           "missing": [s for s in must if s not in got]}
    mi = out["must_include"]
    mi["recall"] = round(mi["found"] / mi["n"], 4) if mi["n"] else None
    mi["recall_top10"] = round(mi["found_top10"] / mi["n"], 4) if mi["n"] else None
    out["cost_usd"] = result.get("cost_usd")
    out["seconds"] = (result.get("timing") or {}).get("total_s")
    out.update(_label_counts(data))
    out["matched_by_company"] = sorted({h for sid, h in zip(ids, hits) if h is not None and h != sid})
    if keys is not None:
        out["not_in_store"] = [sid for sid in labels if sid not in keys]
    return out


def _mean(xs: list[float | None]) -> float | None:
    v = [x for x in xs if x is not None]
    return round(sum(v) / len(v), 4) if v else None


def aggregate(scores: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Means over the ideas whose run finished (counted: precision@k, lenient, coverage, must-include recall), overall
    and per idea type; the others in left_out (id, status); cost_usd_total: every idea's Jev cost."""
    def agg(ss: list[Mapping[str, Any]]) -> dict[str, Any]:
        d: dict[str, Any] = {"ideas": len(ss)}
        for k in KS:
            d[f"p@{k}"] = _mean([s["at"].get(str(k), {}).get("precision") for s in ss])
            d[f"p_lenient@{k}"] = _mean([s["at"].get(str(k), {}).get("precision_lenient") for s in ss])
            d[f"coverage@{k}"] = _mean([s["at"].get(str(k), {}).get("coverage") for s in ss])
        d["must_recall"] = _mean([s["must_include"]["recall"] for s in ss])
        d["must_recall_top10"] = _mean([s["must_include"]["recall_top10"] for s in ss])
        d["cost_usd"] = round(sum(float(s.get("cost_usd") or 0) for s in ss), 6)
        return d
    done = [s for s in scores if counted(s)]
    types = sorted({s.get("type") for s in done if s.get("type")})
    return {"all": agg(done), "by_type": {t: agg([s for s in done if s.get("type") == t]) for t in types},
            "left_out": [{"id": s.get("id"), "status": s.get("status")} for s in scores if not counted(s)],
            "cost_usd_total": round(sum(float(s.get("cost_usd") or 0) for s in scores), 6)}


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v:.0%}"


def report_md(scores: list[Mapping[str, Any]], agg: Mapping[str, Any], *, title: str = "jev-screen eval") -> str:
    """A short Markdown report: the overall line, per type, per idea (with the run's status and its unreviewed
    labels), the ideas left out of the means, and the unlabelled companies to label next."""
    a = agg["all"]
    left = agg.get("left_out") or []
    n_lab = sum(int(s.get("labels") or 0) for s in scores)
    unrev = sum(int(s.get("unreviewed_labels") or 0) for s in scores)
    summ = sum(int(s.get("search_summary_labels") or 0) for s in scores)
    cost = agg.get("cost_usd_total", a["cost_usd"])
    head = f"{a['ideas']} ideas scored" + (f", {len(left)} left out of the means" if left else "")
    lines = [f"# {title}", "",
             f"{head}. P@10 {_pct(a['p@10'])} (lenient {_pct(a['p_lenient@10'])}, labelled "
             f"{_pct(a['coverage@10'])}), P@40 {_pct(a['p@40'])} (labelled {_pct(a['coverage@40'])}), must-include "
             f"recall {_pct(a['must_recall'])} (top 10: {_pct(a['must_recall_top10'])}); Jev ${float(cost):.4f}.",
             "", "Precision counts only labelled companies (right / labelled); lenient counts edge as right. "
             + (f"{unrev} of {n_lab} labels are unreviewed" + (f" ({summ} checked only against a search summary)"
                                                               if summ else "")
                + ": numbers built on them are provisional." if unrev else "Every label is reviewed.")
             + (" Labels were matched by security_id only (the store was not read): the other line of a dual "
                "listing counts as unlabelled." if any("at" in s and "not_in_store" not in s for s in scores) else ""),
             "", "## By idea type", "",
             "| type | ideas | P@10 | P@40 | must-include recall |", "|---|---|---|---|---|"]
    for t, d in agg["by_type"].items():
        lines.append(f"| {t} | {d['ideas']} | {_pct(d['p@10'])} | {_pct(d['p@40'])} | {_pct(d['must_recall'])} |")
    lines += ["", "## By idea", "",
              "| idea | status | run | P@10 (r/e/w/?) | P@40 (r/e/w/?) | must-include | missing | unreviewed labels "
              "| cost |", "|---|---|---|---|---|---|---|---|---|"]
    for s in scores:
        cells = ["-", "-", "-", "-"]
        if "at" in s:
            for i, k in enumerate(KS):
                x = s["at"][str(k)]
                cells[i] = f"{_pct(x['precision'])} ({x['right']}/{x['edge']}/{x['wrong']}/{x['unlabelled']})"
            mi = s["must_include"]
            cells[2:] = [f"{mi['found']}/{mi['n']}", ", ".join(mi["missing"]) or "-"]
        ss = int(s.get("search_summary_labels") or 0)
        rev = f"{s.get('unreviewed_labels') or 0}/{s.get('labels') or 0}" + (f" ({ss} search summary)" if ss else "")
        lines.append(f"| {s['id']} | {s.get('status') or '-'} | {s.get('run_id') or '-'} | {' | '.join(cells)} | "
                     f"{rev} | ${float(s.get('cost_usd') or 0):.4f} |")
    if left:
        lines += ["", "## Left out of the means (the run did not finish)", ""]
        lines += [f"- {s['id']}: {s.get('status') or '-'}" + (f" ({s['error']})" if s.get("error") else "")
                  for s in scores if not counted(s)]
    unknown = {s["id"]: s["not_in_store"] for s in scores if s.get("not_in_store")}
    if unknown:
        lines += ["", "## Label ids the store does not know (check the EXCHANGE:SYMBOL)", ""]
        lines += [f"- {i}: {', '.join(v)}" for i, v in unknown.items()]
    todo = {s["id"]: s["at"]["10"]["unlabelled_ids"] for s in scores if "at" in s and s["at"]["10"]["unlabelled_ids"]}
    if todo:
        lines += ["", "## Unlabelled in the top 10 (label these next)", ""]
        lines += [f"- {i}: {', '.join(v)}" for i, v in todo.items()]
    return "\n".join(lines) + "\n"
