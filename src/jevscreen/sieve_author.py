"""Sieve author fields: what the user's AI may write into an idea's sieve (docs/SIEVE.md, docs/sieve.schema.json).

Author fields (a draft may carry only these): idea, idea_en, seed_terms, facets, facets_zh, target_terms,
should_pass, should_fail, notes. Everything else in a sieve is written by the tool (answers, pins, rules, keyword
learning, `resolved` per checked company, idea_en_reprice).

- should_pass / should_fail are checks, never pins: the companies are always read by layer 2 and reported, and their
  entries are resolved ONCE when written (`resolved`: security_id, company_key, name, method, resolved_at). load_sieve
  turns each resolved entry into an in-memory example {source: 'sieve', want, security_id, company_key, _author:
  True}, so every existing consumer (forced reads, rule trials, keyword mining, the report's checks) works unchanged;
  save_sieve strips them again. A card answer / pin for the same company wins (answer > check).
- Pins come only from card answers or `jevscreen sieve pin` (a card-style example with via 'pin'), never from a
  draft: a draft with include / exclude / pins / examples / rules is refused.
- idea_en is used only for the exact idea it was written for and only when that idea is not English; once a paid run
  of the idea exists, the idea_en that run used is binding (freeze: it is applied, the local model is not asked)
  unless `sieve set --reprice` stored that exact text as idea_en_reprice (the human's yes covers one idea_en). It may not name a company the sieve lists (error); names of big companies are warnings.
"""
from __future__ import annotations

import copy
import datetime as dt
import re
import unicodedata
from typing import Any, Iterable

AUTHOR_FIELDS = ("idea", "idea_en", "seed_terms", "facets", "facets_zh", "target_terms", "should_pass",
                 "should_fail", "notes")
CHECK_LISTS = ("should_pass", "should_fail")
CHECK_MAX, IDEA_EN_MAX, SEED_MAX, SEED_CHARS, NOTES_MAX, WHY_MAX = 30, 300, 15, 60, 2000, 300
PASS_WANTS = ("explicit", "partial")
PIN_FIELDS = ("include", "exclude", "pins", "examples")
LANGS = ("en", "zh", "ja", "ko")
BIG_MCAP_USD = 1e10        # idea_en warnings: names / tickers of companies at least this large
# English words that are also company names or tickers but are ordinary in an idea (no warning)
STOP_WORDS_EN = frozenset("""
harmonic target block access visa unity shell oracle figure snap square match apple amazon meta alphabet
signal zoom slack box dropbox twilio okta elastic confluent datadog cloudflare fortinet palantir sea grab
general national american international global first united capital digital energy power solar data cloud
""".split())
# Ordinary English words (and common acronyms) that are also tickers (Agilent A, onsemi ON, Gartner IT, Allstate ALL,
# ServiceNow NOW ...): never an idea_en error by themselves (the company's name still is)
TICKER_WORDS = frozenset("""
a an and are as at be by do for go he if in is it me my no of on or so the to up us we all any can new now one out own
see two way who why how its our has had not but get got may use via per top big key low max min pro net old
ai ev iot api gpu cpu ar vr ml llm esg saas b2b b2c usa uk eu
""".split())
LIST_ZH = {"should_pass": "应该有（检查）", "should_fail": "不该有（检查）"}
LIST_EN = {"should_pass": "should be there (check)", "should_fail": "should not be there (check)"}
PIN_REFUSED_ZH = "钉选只能在你本人同意后加：jevscreen sieve pin …"
PIN_REFUSED_EN = "Pins need the human's yes: jevscreen sieve pin …"


def _err(code: str, zh: str, en: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "text_zh": zh, "text_en": en, **extra}


def _str_list(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def field_problems(data: dict[str, Any]) -> list[str]:
    """Type problems of the author fields (and their tool-written parts) in a stored sieve, for validate_sieve."""
    errs: list[str] = []
    if data.get("idea_en") is not None and (not isinstance(data["idea_en"], str)
                                            or len(data["idea_en"]) > IDEA_EN_MAX):
        errs.append(f"idea_en must be a string of at most {IDEA_EN_MAX} characters")
    st = data.get("seed_terms")
    if st is not None:
        if not isinstance(st, dict):
            errs.append("seed_terms must be an object {lang: [terms]}")
        else:
            for lang, terms in st.items():
                if lang not in LANGS or not _str_list(terms) or len(terms) > SEED_MAX \
                        or any(len(t) > SEED_CHARS for t in terms):
                    errs.append(f"seed_terms.{lang}: language one of {', '.join(LANGS)}, at most {SEED_MAX} terms "
                                f"of at most {SEED_CHARS} characters")
    if data.get("notes") is not None and (not isinstance(data["notes"], str) or len(data["notes"]) > NOTES_MAX):
        errs.append(f"notes must be a string of at most {NOTES_MAX} characters")
    rp = data.get("idea_en_reprice")
    if rp is not None and not isinstance(rp, bool) and not (isinstance(rp, str) and len(rp) <= IDEA_EN_MAX):
        errs.append("idea_en_reprice must be the approved idea_en text")
    seen: dict[str, str] = {}
    for lst in CHECK_LISTS:
        v = data.get(lst)
        if v is None:
            continue
        if not isinstance(v, list) or len(v) > CHECK_MAX:
            errs.append(f"{lst} must be a list of at most {CHECK_MAX} entries")
            continue
        for i, e in enumerate(v):
            if not isinstance(e, dict) or not isinstance(e.get("company"), str) or not e["company"].strip():
                errs.append(f"{lst}[{i}] needs 'company' (a ticker, code or name)")
                continue
            if lst == "should_pass" and e.get("want", "explicit") not in PASS_WANTS:
                errs.append(f"{lst}[{i}].want must be explicit or partial")
            if e.get("why") is not None and (not isinstance(e["why"], str) or len(e["why"]) > WHY_MAX):
                errs.append(f"{lst}[{i}].why must be a string of at most {WHY_MAX} characters")
            r = e.get("resolved")
            if r is not None and (not isinstance(r, dict) or not (r.get("security_id") or r.get("company_key"))):
                errs.append(f"{lst}[{i}].resolved must name security_id or company_key")
            ident = _entry_key(e)
            if ident in seen and seen[ident] != lst:
                errs.append(f"{e['company']}: in both should_pass and should_fail")
            seen.setdefault(ident, lst)
    return errs


def _entry_key(e: dict[str, Any]) -> str:
    r = e.get("resolved") or {}
    return r.get("company_key") or r.get("security_id") or unicodedata.normalize("NFKC", e["company"]).strip().casefold()


def author_examples(sieve: dict[str, Any]) -> list[dict[str, Any]]:
    """The in-memory examples of the resolved should_pass / should_fail entries (source 'sieve', _author True),
    minus companies that already have an example (a card answer or a legacy check wins)."""
    have = {k for ex in sieve.get("examples") or [] if isinstance(ex, dict) and not ex.get("_author")
            for k in (ex.get("company_key"), ex.get("security_id")) if k}
    out = []
    for lst in CHECK_LISTS:
        for e in sieve.get(lst) or []:
            r = e.get("resolved") if isinstance(e, dict) else None
            if not r:
                continue
            if r.get("company_key") in have or r.get("security_id") in have:
                continue
            out.append({"security_id": r.get("security_id"), "company_key": r.get("company_key"),
                        "name": r.get("name") or e.get("company"),
                        "want": (e.get("want") or "explicit") if lst == "should_pass" else "no",
                        "source": "sieve", "pin": False, "why": e.get("why"), "list": lst, "_author": True})
    return out


def strip_author(examples: Iterable[Any]) -> list[Any]:
    return [ex for ex in examples or [] if not (isinstance(ex, dict) and ex.get("_author"))]


def conflicts(sieve: dict[str, Any]) -> list[dict[str, Any]]:
    """Checks that contradict a card answer / pin of the same company (the answer wins: a warning)."""
    cards = {}
    for ex in sieve.get("examples") or []:
        if isinstance(ex, dict) and ex.get("source", "card") == "card" and not ex.get("_author"):
            for k in (ex.get("company_key"), ex.get("security_id")):
                if k:
                    cards[k] = ex
    out = []
    for lst in CHECK_LISTS:
        for e in sieve.get(lst) or []:
            r = e.get("resolved") or {}
            ex = cards.get(r.get("company_key")) or cards.get(r.get("security_id"))
            if ex is None:
                continue
            yes = ex.get("want") in PASS_WANTS
            if ex.get("want") != "unsure" and yes != (lst == "should_pass"):
                out.append(_err("check_vs_answer", f"你的回答和 AI 的草稿矛盾（{e['company']}）：以你的回答为准",
                                f"The human's answer contradicts the draft ({e['company']}): the answer wins"))
    return out


# ------------------------------------------------------------------------------------------------ drafts and merges

def validate_draft(draft: Any) -> list[dict[str, Any]]:
    """Errors of an author draft (a JSON object of author fields); empty when it may be merged."""
    if not isinstance(draft, dict):
        return [_err("not_object", "草稿必须是一个 JSON 对象", "The draft must be a JSON object")]
    errs = []
    for k in draft:
        if k in PIN_FIELDS:
            errs.append(_err("pins_refused", PIN_REFUSED_ZH, PIN_REFUSED_EN, field=k))
        elif k == "rules":
            errs.append(_err("rules_refused", "规则只能通过校准卡的试验采用，不能写进草稿",
                             "Rules are adopted only through the card trials, never from a draft", field=k))
        elif k not in AUTHOR_FIELDS and k not in ("format",):
            errs.append(_err("unknown_field", f"草稿里不能有「{k}」（只能写：{', '.join(AUTHOR_FIELDS)}）",
                             f"Field {k!r} is not an author field ({', '.join(AUTHOR_FIELDS)})", field=k))
    body = {k: v for k, v in draft.items() if k in AUTHOR_FIELDS and v is not None}
    for lst in CHECK_LISTS:
        for e in body.get(lst) or []:
            if isinstance(e, dict) and "resolved" in e:
                errs.append(_err("tool_field", "resolved 由工具写，草稿里不要带", "'resolved' is written by the tool",
                                 field=lst))
    for p in field_problems(body):
        errs.append(_err("schema", p, p))
    if body.get("idea") is not None and not isinstance(body["idea"], str):
        errs.append(_err("schema", "idea 必须是字符串", "idea must be a string"))
    for f in ("facets", "facets_zh"):
        v = body.get(f)
        if v is not None and (not isinstance(v, dict) or not all(isinstance(x, str) for x in v.values())):
            errs.append(_err("schema", f"{f} 必须是字符串对象", f"{f} must be an object of strings"))
    return errs


def _merge(target: Any, patch: Any) -> Any:
    """RFC 7386 JSON merge patch."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = dict(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = _merge(out.get(k), v)
    return out


def migrate_legacy(sieve: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Legacy source-'sieve' examples (checks written into examples by hand) moved into should_pass / should_fail
    with their ids as `resolved` (method 'legacy'). Returns (sieve', moved)."""
    sv = sieve
    keep, moved = [], 0
    for ex in sv.get("examples") or []:
        if isinstance(ex, dict) and ex.get("source") == "sieve" and not ex.get("_author") \
                and ex.get("want") in PASS_WANTS + ("no",):
            lst = "should_fail" if ex["want"] == "no" else "should_pass"
            entry = {"company": ex.get("security_id") or ex.get("company_key"),
                     "resolved": {"security_id": ex.get("security_id"), "company_key": ex.get("company_key"),
                                  "name": ex.get("name"), "method": "legacy", "resolved_at": now_iso()}}
            if lst == "should_pass":
                entry["want"] = ex["want"]
            sv.setdefault(lst, [])
            if not any(_entry_key(e) == _entry_key(entry) for e in sv[lst] if isinstance(e, dict)):
                sv[lst].append(entry)
            moved += 1
        else:
            keep.append(ex)
    sv["examples"] = keep
    return sv, moved


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def merge_author(sieve: dict[str, Any], draft: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(sieve', diff): the draft's author fields merged into a copy of the sieve (merge patch; lists replace, check
    entries keep their stored `resolved` when the company text is unchanged), legacy checks migrated. diff:
    [{field, before, after}] of the fields that changed (check lists as added / removed companies)."""
    sv = copy.deepcopy(sieve)
    sv["examples"] = strip_author(sv.get("examples"))
    sv, moved = migrate_legacy(sv)
    diff: list[dict[str, Any]] = []
    if moved:
        diff.append({"field": "examples", "migrated": moved})
    for k in AUTHOR_FIELDS:
        if k not in draft or k == "idea":
            continue
        before = copy.deepcopy(sv.get(k))
        v = draft[k]
        if k in CHECK_LISTS:
            old = {unicodedata.normalize("NFKC", e["company"]).strip().casefold(): e for e in before or []
                   if isinstance(e, dict)}
            new = []
            for e in v or []:
                e2 = {kk: vv for kk, vv in e.items() if kk != "resolved"}
                prev = old.get(unicodedata.normalize("NFKC", e["company"]).strip().casefold())
                if prev and prev.get("resolved"):
                    e2["resolved"] = prev["resolved"]
                new.append(e2)
            after = new
        elif v is None:
            after = None
        elif isinstance(v, dict):
            after = _merge(before, v) or None
        else:
            after = copy.deepcopy(v)
        if after != before:
            if k in CHECK_LISTS:
                bn = {e["company"] for e in before or []}
                an = {e["company"] for e in after or []}
                diff.append({"field": k, "added": sorted(an - bn), "removed": sorted(bn - an)})
            else:
                diff.append({"field": k, "before": before, "after": after})
        if after is None and k in CHECK_LISTS:
            after = []
        sv[k] = after
    return sv, diff


def resolve_checks(sieve: dict[str, Any], index: Any, *, prefer: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Resolve every check entry without `resolved` (score 1.0 only: id / symbol / code / exact name). Entries that
    do not resolve uniquely stay unresolved; returns their errors with up to 5 candidates."""
    from . import names
    errs = []
    for lst in CHECK_LISTS:
        for e in sieve.get(lst) or []:
            if e.get("resolved"):
                continue
            m = names.resolve(index, e["company"], prefer=prefer)
            if m.status == "exact":
                e["resolved"] = {"security_id": m.security_id, "company_key": m.company_key, "name": m.name,
                                 "method": m.method, "resolved_at": now_iso()}
                continue
            cands = [f"{c['security_id']}（{c['name']}）" for c in m.candidates]
            errs.append(_err("unresolved", f"「{e['company']}」对不上唯一的公司" + (f"：可能是 {'、'.join(cands)}；请用代码"
                                                                            if cands else "：请用交易所代码"),
                             f"{e['company']!r} does not name one company" + (f": {', '.join(cands)}; use a ticker"
                                                                           if cands else ": use an exchange ticker"),
                             list=lst, company=e["company"], candidates=m.candidates))
    return errs


def pin_example(match: Any, want: str, *, now: str | None = None) -> dict[str, Any]:
    """A card-style pin written by `jevscreen sieve pin` (the human's own yes / no)."""
    return {"security_id": match.security_id, "company_key": match.company_key, "name": match.name, "want": want,
            "chip": None, "source": "card", "pin": True, "via": "pin", "at": now or now_iso()}


# ------------------------------------------------------------------------------------------------------ idea_en

_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9&']*[A-Za-z0-9]|[A-Za-z]")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(unicodedata.normalize("NFKC", text or ""))


def _name_hits(text: str, name: str) -> bool:
    from .calib import _short_name
    s = _short_name(name)
    if len(s) < 3:
        return False
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(s)}(?![A-Za-z0-9])", unicodedata.normalize("NFKC", text or ""),
                     re.I) is not None


def sieve_companies(sieve: dict[str, Any]) -> list[dict[str, Any]]:
    """[{name, security_id, company_key}] of every company the sieve names (checks and examples)."""
    out = []
    for lst in CHECK_LISTS:
        for e in sieve.get(lst) or []:
            r = e.get("resolved") or {}
            out.append({"name": r.get("name") or e.get("company"), "security_id": r.get("security_id"),
                        "company_key": r.get("company_key"), "text": e.get("company")})
    for ex in sieve.get("examples") or []:
        if isinstance(ex, dict) and not ex.get("_author"):
            out.append({"name": ex.get("name"), "security_id": ex.get("security_id"),
                        "company_key": ex.get("company_key"), "text": None})
    return out


_TICKERISH = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,6}")


def _ticker_hits(sym: str, toks: set[str]) -> bool:
    """A ticker counts only as the exact upper-case token, at least 2 characters, and not an ordinary word
    (TICKER_WORDS, STOP_WORDS_EN): 'a', 'on', 'it', 'all', 'now' in an idea_en are words, not Agilent / onsemi ..."""
    return (len(sym) >= 2 and sym.upper() == sym and not sym.isdigit() and sym in toks
            and sym.casefold() not in TICKER_WORDS and sym.casefold() not in STOP_WORDS_EN)


def idea_en_errors(idea_en: str | None, sieve: dict[str, Any]) -> list[dict[str, Any]]:
    """idea_en names / tickers / aliases of a company this sieve lists (it would steer the model to that company).
    Names match case-insensitively as whole words; tickers only as the exact upper-case token (_ticker_hits)."""
    if not idea_en:
        return []
    toks = {t for t in _tokens(idea_en) if not t.isdigit()}
    out, seen = [], set()
    for c in sieve_companies(sieve):
        sym = (c.get("security_id") or "").rpartition(":")[2]
        text = (c.get("text") or "").strip()
        tickerish = ":" in text or bool(_TICKERISH.fullmatch(text)) or (sym and text.casefold() == sym.casefold())
        hit = None
        if c.get("name") and _name_hits(idea_en, c["name"]):
            hit = c["name"]
        elif sym and _ticker_hits(sym, toks):
            hit = sym
        elif text and tickerish and _ticker_hits(text.rpartition(":")[2], toks):
            hit = text.rpartition(":")[2]
        elif text and not tickerish and not text.isdigit() and len(text) >= 2 and _name_hits(idea_en, text):
            hit = text
        if hit and hit not in seen:
            seen.add(hit)
            out.append(_err("idea_en_names_company", f"idea_en 里有你列出的公司「{hit}」：模型会被引向它，请删掉",
                            f"idea_en names {hit!r}, a company this sieve lists: remove it", word=hit))
    return out


def idea_en_warnings(idea_en: str | None, sieve: dict[str, Any], big: Iterable[tuple[str, str]]) -> list[dict]:
    """Words of idea_en that are also names (>= 5 characters) or tickers (>= 4 letters) of big companies ((name,
    ticker) of companies >= BIG_MCAP_USD), minus STOP_WORDS_EN and the sieve's own seed / target terms."""
    if not idea_en:
        return []
    own = {w.casefold() for lang_terms in list((sieve.get("seed_terms") or {}).values())
           + list((sieve.get("target_terms") or {}).values()) for t in lang_terms or [] for w in _tokens(t)}
    toks = [t for t in _tokens(idea_en) if not t.isdigit()]
    low = {t.casefold(): t for t in toks}
    out, seen = [], set()
    from .calib import _short_name
    for name, ticker in big:
        for cand, is_ticker in ((_short_name(name or ""), False), (ticker or "", True)):
            c = cand.casefold()
            if not c or c in STOP_WORDS_EN or c in own or c in seen:
                continue
            if is_ticker:
                if len(cand) < 4 or not cand.isalpha() or c not in low or low[c] != low[c].upper():
                    continue
            elif len(cand) < 5 or " " in cand.strip() and not _name_hits(idea_en, cand) or \
                    (" " not in cand.strip() and c not in low):
                continue
            word = low.get(c, cand)
            seen.add(c)
            out.append(_err("idea_en_company_word", f"「{word}」也是一家公司的名字：会不会把模型引向它？可以改成小写或换个说法"
                            "（只是提醒）", f"{word!r} is also a company name: could it steer the model? Use lower "
                            "case or another word (warning only)", word=word))
    return out


def reprice_approved(sieve: dict[str, Any] | None) -> bool:
    """Whether `sieve set --reprice` (the human's yes) approved the sieve's current idea_en. idea_en_reprice holds
    the approved text, so the yes covers that one text only: a later rewrite is frozen again (a legacy `true` never
    approves anything)."""
    rp = (sieve or {}).get("idea_en_reprice")
    en = ((sieve or {}).get("idea_en") or "").strip()
    return isinstance(rp, str) and bool(en) and rp.strip() == en


def author_keywords(sieve: dict[str, Any] | None, idea: str, *, frozen: tuple[bool, str | None] = (False, None)
                    ) -> dict[str, Any] | None:
    """What the sieve's author fields give screen.resolve_keywords: {'idea_en' (or None), 'seed_terms' {lang: [...]},
    'source' ('sieve' | 'frozen' | None), 'frozen' bool, 'warnings' [text]}. idea_en applies only when the sieve was
    written for exactly this idea and the idea is not English; frozen = (a paid run of this idea exists, the idea_en
    it used): a different sieve idea_en is then replaced by the frozen one (source 'frozen', frozen True; None when
    that run used the idea itself; the local model is not asked) unless reprice_approved."""
    if not sieve:
        return None
    from . import screen
    seeds = {lang: list(v) for lang, v in (sieve.get("seed_terms") or {}).items() if v}
    out: dict[str, Any] = {"idea_en": None, "seed_terms": seeds, "source": None, "frozen": False, "warnings": []}
    en = (sieve.get("idea_en") or "").strip()
    if en:
        if (sieve.get("idea") or "").strip() != idea.strip():
            out["warnings"].append("The sieve's idea_en was written for another idea text; it is ignored.")
        elif not screen.needs_translation(idea):
            out["warnings"].append("The idea is English; the sieve's idea_en is ignored.")
        elif frozen[0] and en != (frozen[1] or "") and not reprice_approved(sieve):
            out["warnings"].append("idea_en 已冻结：这个想法已经付费跑过，沿用上次的 idea_en；要改请用 jevscreen sieve set "
                                   "--reprice（下次第一步重问一遍，需要你同意） / idea_en is frozen: the last paid "
                                   "run's idea_en is kept; changing it needs sieve set --reprice (the human's yes)")
            out.update(idea_en=frozen[1], source="frozen", frozen=True)
        else:
            out.update(idea_en=en, source="sieve")
    return out
