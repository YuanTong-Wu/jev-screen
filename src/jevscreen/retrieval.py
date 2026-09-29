"""Retrieval terms for every document language, and the second search (plan step 3; both levers default OFF).

`--lang-terms` (lever): resolve_keywords yields excerpt terms for every document language (en / zh / ja / ko) even
when the caller supplied idea_en (quickstart, `eval run`) or the idea is English. Today the local keyword model is
skipped in those cases, so a Japanese or Korean filing is searched with no term at all and a Chinese one only with
the idea's own long compound. With the lever the local model (free, offline; jevscreen.keywords) is asked for the
terms only: its idea_en is never used, so the Jev questions (and every L1 answer) stay the same. A language whose
terms came from the user's flags, the sieve's seed terms or the model already is left as it is (en keeps the words
of idea_en); only the fallback languages (the idea's own fragments, or nothing) are filled. Latin anchors of the
idea (HBM, SiC, GLP-1, eVTOL: written the same way in CJK filings) are added to zh / ja / ko. Generic fragments
(核心零部件, 供应商, 製品, 제품, AI, and the two-character pieces screen.excerpt_terms slides over a long Chinese
compound) count as weak terms (screen.WEAK_TERM_WEIGHT), never as full ones. The L2 excerpts of documents whose
chosen paragraphs change are new L2 items (paid, cached); everything else is a cache hit.

`--second-search` (lever): after L2, the companies L1 judged core whose annual-report excerpts L2 found
insufficient get one more L2 read (the same question) over OTHER passages of the same filing: up to three
paragraphs matching the widened terms that the first excerpts did not show. The answers form their own layer
(screen_results layer `l2_second`); an explicit or partial answer raises the row to partial at most (never
explicit, never the high-confidence tier), marked `l2_second_search: raised` so the page can say that the system
inferred it from another passage. Rows without such a passage are not read again (`no_new_text`).

Pure helpers except `layer` (Jev through the caller's factory); no store access, no network.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterable

KEY = "l2_second"                   # screen_results layer of the second-search answers
READ_MAX = 150                      # at most this many rows are searched again (L1 p_core order)
MAX_PARAS = 3                       # paragraphs per second-search text
SHARE_EST = 0.15                    # dry-run estimate: share of the L2 items that are L1 core + L2 insufficient
LANGS = ("en", "zh", "ja", "ko")
CJK = ("zh", "ja", "ko")

# words that say nothing about what an idea is about: a term made only of these is weak (应用 is left out: 应用材料 is
# Applied Materials)
GENERIC: dict[str, frozenset[str]] = {
    "zh": frozenset("""零部件 部件 配件 组件 供应商 供应 制造商 生产商 厂商 厂家 企业 公司 上市公司 核心 关键 主要 相关 设备 装备
        系统 产品 技术 服务 解决方案 方案 平台 材料 业务 领域 行业 市场 产业 产业链 上游 下游 龙头 国产 国产替代 替代 概念
        标的 智能 高端 新型 研发 生产 销售 制造 提供 中国 国内 全球 模块 部分 环节 领先 发展 管理 以及 用于 相关产品""".split()),
    "ja": frozenset("""部品 製品 事業 会社 企業 システム サービス 装置 機器 技術 材料 製造 販売 開発 提供 関連 主要 市場 分野
        業界 ソリューション メーカー 供給 日本 国内 グローバル""".split()),
    "ko": frozenset("""부품 제품 사업 회사 기업 시스템 서비스 장비 기술 소재 제조 판매 개발 공급 관련 주요 시장 분야 산업
        솔루션 업체 한국 국내 글로벌""".split()),
}
GENERIC_LATIN = frozenset({"AI", "IT", "IOT", "R&D", "US", "USA", "EU", "UK", "B2B", "B2C", "OEM", "API", "IP"})
_HAN = r"㐀-䶿一-鿿豈-﫿"
_FUNCTION = frozenset("的和与及或在为是等之了对于把被将向从其各该此并也都のをにはがとで")
_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]+(?:-[A-Za-z0-9]+)?(?![A-Za-z0-9])")


def _nfkc(t: str) -> str:
    return unicodedata.normalize("NFKC", str(t or "")).strip()


def _dedup(xs: Iterable[str]) -> list[str]:
    seen, out = set(), []
    for x in xs:
        x = _nfkc(x)
        k = x.casefold()
        if x and k not in seen:
            seen.add(k)
            out.append(x)
    return out


def is_generic(term: str, lang: str) -> bool:
    """True when `term` names no specific product: a generic Latin acronym (AI, OEM), or a zh / ja / ko term made
    wholly of generic words (GENERIC[lang]) and particles (核心零部件 = 核心 + 零部件, 设备供应商, 製品, 제품). A term
    with anything else left is a content term, a single Han character included (光模块, 服务器, 铀生产, 云服务: the
    character carries the meaning). English terms are never judged here (en keeps its words)."""
    t = _nfkc(term)
    if not t:
        return True
    if t.upper() in GENERIC_LATIN:
        return True
    if lang not in CJK:
        return False
    words = GENERIC.get(lang, frozenset())
    t = "".join(ch for ch in t if not ch.isspace())
    if not t:
        return True
    longest = max((len(w) for w in words), default=1)
    ok = [True] + [False] * len(t)        # ok[i]: t[:i] splits into generic words and particles
    for i in range(1, len(t) + 1):
        if t[i - 1] in _FUNCTION and ok[i - 1]:
            ok[i] = True
            continue
        ok[i] = any(ok[i - n] and t[i - n:i] in words for n in range(1, min(longest, i) + 1))
    return ok[len(t)]


def latin_anchors(text: str | None) -> list[str]:
    """Latin tokens a CJK filing writes the same way: acronyms with two or more capitals / digits and a capital
    (HBM, SiC, LNG, GLP-1, eVTOL, RV), generic ones (AI, OEM: GENERIC_LATIN) excluded."""
    out = []
    for w in _TOKEN.findall(text or ""):
        caps = sum(1 for ch in w if ch.isupper() or ch.isdigit())
        if caps >= 2 and any(ch.isupper() for ch in w) and w.upper() not in GENERIC_LATIN:
            out.append(w)
    return _dedup(out)


def terms_sha(terms: dict[str, list[str]] | None, weak: dict[str, list[str]] | None) -> str | None:
    """sha256 (16 hex) of the excerpt terms (and weak terms) a run searched with; None when there are none."""
    if not terms and not weak:
        return None
    body = {"terms": {k: list(v) for k, v in sorted((terms or {}).items()) if v},
            "weak": {k: list(v) for k, v in sorted((weak or {}).items()) if v}}
    raw = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def every_language(cfg, idea: str, info: dict[str, Any], terms: dict[str, list[str]], *,
                   user: dict[str, list[str]], seeds: dict[str, list[str]], keywords_fn: Callable
                   ) -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, Any]]:
    """(terms, weak, summary) with terms for every document language (the --lang-terms lever; see the module).

    `terms` is what screen.resolve_keywords chose by its usual precedence; `info` its info (idea_en, status,
    generated). The local model (keywords_fn(cfg, idea)) is asked only when it has not run already; its idea_en is
    ignored. Per language: the user's flags are kept as given; terms from the sieve's seeds or the model are kept
    (their generic ones weak); a fallback language (the idea's own fragments, or none) gets the model's terms added (zh / ja / ko also the
    idea's Latin anchors; ja without model terms borrows the Han-only zh terms as weak ones). Generic terms
    (is_generic) move to `weak`. Never raises: a model failure keeps the anchors and is recorded in the summary."""
    summary: dict[str, Any] = {"status": "model", "model": info.get("model"), "cached": info.get("cached"),
                               "error": None, "added": {}, "weak": {}}
    gen = info.get("generated") if info.get("status") == "generated" else None
    if not gen:
        try:
            out = keywords_fn(cfg, idea)
        except Exception as e:  # noqa: BLE001 - KeywordsUnavailable or any local-model failure: anchors only
            gen = {}
            summary.update(status="unavailable", error=f"{type(e).__name__}: {str(e)[:300]}")
        else:
            out = out if isinstance(out, dict) else {}
            kw = out.get("keywords") if isinstance(out.get("keywords"), dict) else {}
            gen = {lang: _dedup(kw.get(lang) or []) for lang in LANGS}
            summary.update(model=out.get("model"), cached=out.get("cached"))
    gen = {lang: _dedup((gen or {}).get(lang) or []) for lang in LANGS}
    anchors = latin_anchors(" ".join(x for x in (info.get("idea_en"), idea) if x))
    new: dict[str, list[str]] = {}
    weak: dict[str, list[str]] = {}
    for lang in LANGS:
        base = _dedup(terms.get(lang) or [])
        if user.get(lang) or (lang == "en" and base):
            new[lang] = base            # the user's flags as given; en: the words of idea_en (or of an English idea)
            continue
        if seeds.get(lang) or (info.get("status") == "generated" and gen.get(lang)):
            # already the sieve's or the model's own terms: nothing added, generic ones weak
            new[lang] = [t for t in base if not is_generic(t, lang)]
            wk = [t for t in base if is_generic(t, lang)]
            if wk:
                weak[lang] = summary["weak"][lang] = wk
            continue
        add = list(gen.get(lang) or [])
        wk: list[str] = []
        if lang in CJK:
            add += anchors
            if lang == "ja" and not gen.get("ja"):
                zh_han = [t for t in (gen.get("zh") or terms.get("zh") or [])
                          if re.fullmatch(f"[{_HAN}]{{2,12}}", _nfkc(t))]
                wk += zh_han            # Japanese filings share many kanji compounds: weak evidence only
        merged = _dedup(base + add)
        strong = [t for t in merged if not is_generic(t, lang)]
        wk = _dedup([t for t in merged if is_generic(t, lang)] + [t for t in wk if t.casefold() not in
                                                                 {s.casefold() for s in strong}])
        new[lang], weak[lang] = strong, wk
        added = [t for t in strong if t.casefold() not in {b.casefold() for b in base}]
        if added:
            summary["added"][lang] = added
        if wk:
            summary["weak"][lang] = wk
    return new, {k: v for k, v in weak.items() if v}, summary


def model_ok(summary: dict[str, Any] | None) -> bool:
    """Whether every_language got the local model's terms (False when it fell back to the Latin anchors)."""
    return (summary or {}).get("status") != "unavailable"


def unavailable_warning(summary: dict[str, Any] | None) -> str:
    """The run warning when the local keyword model gave no terms for --lang-terms / --second-search (zh / en)."""
    err = (summary or {}).get("error") or "unknown error"
    return ("本地检索词模型不可用：各语言的检索词只用了想法里的英文缩写，检索开关没有真正起作用，这次结果不能说明它的效果（可用 "
            "jevscreen keywords 检查） / The local keyword model is unavailable (" + err + "): the terms for each "
            "language fell back to the idea's Latin acronyms, so this run does not measure the retrieval lever "
            "(check with jevscreen keywords)")


# ---------------------------------------------------------------------------------------------------------------
# second search

def _shown_keys(excerpts: Iterable[dict[str, Any]] | None) -> set[str]:
    """20-character pieces (every 40 characters, spaces removed) of the excerpts L2 already read."""
    out: set[str] = set()
    for e in excerpts or []:
        t = re.sub(r"\s+", "", str((e or {}).get("text") or "")).replace("…", "")
        out.update(t[i:i + 20] for i in range(0, max(1, len(t) - 19), 40) if len(t[i:i + 20]) == 20)
    return out


def _shown(para: str, keys: set[str]) -> bool:
    flat = re.sub(r"\s+", "", para)
    return any(k in flat for k in keys)


def paragraphs(text: str, *, terms: list[str], weak: list[str], lang: str | None, shown: Iterable[dict] | None,
               max_n: int = MAX_PARAS) -> list[str]:
    """Up to max_n paragraphs of `text` that the first L2 excerpts (`shown`) did not use and that match the terms:
    score = distinct full terms + WEAK_TERM_WEIGHT x distinct weak terms, at least 1; best first (ties -> earlier),
    returned in document order, each cut to the excerpt size around its hits (screen._keyword_window)."""
    from . import screen
    cjk = lang in screen.CJK_LANGS
    max_chars = screen.EXCERPT_MAX_CHARS_CJK if cjk else screen.EXCERPT_MAX_CHARS
    paras = screen.split_paragraphs(text, screen.EXCERPT_MIN_PARA_CJK if cjk else screen.EXCERPT_MIN_PARA,
                                    split_long=max_chars if cjk else None)
    wset = {_nfkc(t).lower() for t in weak or () if _nfkc(t)}
    pats = [screen._term_pattern(t) for t in terms or () if _nfkc(t) and _nfkc(t).lower() not in wset]
    wpats = [screen._term_pattern(t) for t in sorted(wset)]
    if not pats and not wpats:
        return []
    keys = _shown_keys(shown)
    scored = []
    for j, p in enumerate(paras):
        if _shown(p, keys) or (cjk and screen.figure_heavy(p)):
            continue
        s = sum(1 for x in pats if x.search(p)) + screen.WEAK_TERM_WEIGHT * sum(1 for x in wpats if x.search(p))
        if s >= 1:
            scored.append((-s, j))
    pick = sorted(j for _s, j in sorted(scored)[:max_n])
    tlang = lang if cjk else None
    return [screen._keyword_window(paras[j], pats + wpats, max_chars, tlang) for j in pick]


def doc_text(doc: dict[str, Any] | None) -> str | None:
    """The text L2 reads for a document (the summary companion's overview first), as screen._l2_input reads it."""
    if not doc or not doc.get("text_path"):
        return None
    try:
        text = Path(doc["text_path"]).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    comp = doc.get("companion")
    if text and comp and comp.get("text_path"):
        try:
            ctext = Path(comp["text_path"]).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            ctext = ""
        if ctext:
            text = ctext + "\n\n" + text
    return text or None


def second_input(first: dict[str, Any], doc: dict[str, Any] | None, terms: dict[str, list[str]],
                 weak: dict[str, list[str]]) -> dict[str, Any] | None:
    """The second-search L2 input of one company (the widened terms in its document's language), or None when its
    filing has no unused paragraph matching them. `first` is its first L2 input (screen._l2_input)."""
    from . import screen
    if (first or {}).get("evidence") != "annual_report":
        return None
    text = doc_text(doc)
    lang = first.get("lang")
    if not text or not lang:
        return None
    lterms = list(screen.terms_for(terms, lang))
    lweak = list(screen.terms_for(weak, lang))
    pieces = [p for p in screen.excerpt_terms(lterms, lang) if p not in lterms]
    paras = paragraphs(text, terms=lterms, weak=lweak + pieces, lang=lang, shown=first.get("excerpts"))
    if not paras:
        return None
    label = screen.source_label(first.get("source_id"))
    tag = (f"[annual report excerpts: {label} {first.get('form') or 'filing'} filed {first.get('filing_date') or 'date unknown'}; "
           f"language {lang}; other passages of the same filing, found by a second search]")
    body = tag + "\n\n" + "\n\n[...]\n\n".join(paras)
    all_terms = lterms + lweak + pieces
    return {**{k: first.get(k) for k in ("evidence_url", "input_tier", "input_source", "source_id", "form", "lang",
                                          "doc_id", "filing_date", "summary", "stale", "age_days")},
            "text": body, "excerpts": [{"kind": "second_search", "text": p} for p in paras],
            "evidence": "annual_report", "keyword_hit": True,
            "evidence_excerpt": screen.truncate(paras[0], screen.OUTPUT_EXCERPT_CHARS,
                                                lang if lang in screen.CJK_LANGS else None),
            "evidence_sha": screen.evidence_sha(body),
            "matched_terms": screen.matched_terms(" ".join(paras), all_terms), "note": None}


def candidates(l1_res: dict[str, dict], l2_res: dict[str, dict], inputs: dict[str, dict], by_key: dict[str, dict],
               skip: Iterable[str] = ()) -> list[str]:
    """The rows the second search reads again: L1 core (its label) and L2 insufficient on annual-report text, in
    L1 p_core order (then market cap, security id), at most READ_MAX."""
    from . import screen
    skip = set(skip)
    keys = [k for k, r in l2_res.items() if k not in skip and k in by_key and r.get("status") == "ok"
            and r.get("label") == "insufficient" and (inputs.get(k) or {}).get("evidence") == "annual_report"
            and (l1_res.get(k) or {}).get("label") == "core"]
    keys.sort(key=lambda k: (-(screen.p_core_of(l1_res.get(k)) or 0.0), -(by_key[k].get("market_cap_usd") or 0),
                             by_key[k].get("security_id") or ""))
    return keys[:READ_MAX]


def decide(l2_label: str | None, answer: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """(label, state) after the second search: an insufficient row whose second read is explicit or partial becomes
    partial ('raised'); otherwise it keeps its label ('kept'; None when it was not read again)."""
    if l2_label != "insufficient" or not answer:
        return l2_label, None
    if answer.get("status", "ok") == "ok" and answer.get("label") in ("explicit", "partial"):
        return "partial", "raised"
    return l2_label, "kept"


def layer(make, items: list, question, remaining: float, clients: dict[str, Any], notes: list[str], tell=None,
          read_offset: int = 0) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """({company_key: answer}, layer info): ONE read of each second-search item under the L2 question (read index
    `read_offset`, so a noise rerun draws it fresh too). Skipped with a note when the cache-aware estimate exceeds
    `remaining`; never raises for Jev errors (they end the layer early: partial)."""
    import dataclasses
    from . import screen
    info: dict[str, Any] = {"items": len(items), "status": "ok", "skipped": None, "cost_usd": 0.0, "requests": 0,
                            "estimate_usd": None, "read": read_offset}
    out: dict[str, dict[str, Any]] = {}
    if not items:
        return out, info
    try:
        clients[KEY] = client = make(KEY, max(0.0, remaining), False)
    except Exception as e:  # noqa: BLE001
        if not screen._is_error(e, "JevUnavailable"):
            raise
        info.update(status="skipped", skipped="jev_unavailable")
        notes.append(f"二次检索跳过：AI 服务不可用 / second search skipped: Jev unavailable ({type(e).__name__})")
        return out, info
    q = dataclasses.replace(question, read=read_offset) if read_offset else question
    est = screen._estimate_uncached(client, items, q) or {}
    info["estimate_usd"] = round(float(est.get("est_cost_usd") or 0.0), 6)
    if info["estimate_usd"] > remaining + 1e-12:
        info.update(status="skipped", skipped="budget")
        notes.append("二次检索跳过：预算不够，这些公司仍留在待核实名单 / second search skipped: not enough budget; these "
                     "companies stay unverified")
        return out, info
    spent0, sent0 = screen._client_stats(client)
    if tell is not None:
        tell(KEY, 0, len(items), getattr(client, "spent_usd", None))
    got, stop, err = screen._call_classify(client, items, q)
    if stop:
        notes.append(f"二次检索没读完（{stop}） / second search incomplete ({err})")
    for it, r in zip(items, got):
        if r.get("status") == "ok":
            out[it.item_id] = r
    spent1, sent1 = screen._client_stats(client)
    info.update(cost_usd=round(spent1 - spent0, 6), requests=sent1 - sent0, status="partial" if stop else "ok",
                answered=len(out))
    return out, info
