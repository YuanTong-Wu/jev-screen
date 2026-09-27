"""Idea -> ranked stock list: local pre-screen, Jev layer 1 on descriptions, Jev layer 2 on annual-report excerpts,
market-cap filter, a few dozen names out.

Rules (see docs/DATA_RULES.md and docs/REFERENCE.md "Screening"):
- The store is only opened in short sessions (one read-only session for the universe, one for output fundamentals,
  short write sessions for screen_runs / screen_results). No connection is held while Jev requests are in flight.
- Missing values stay NULL (None): no description -> reported, not screened; no revenue history -> CAGR NULL.
- Revenue growth is computed in LOCAL currency from fundamentals_annual only; USD FY values are never mixed in.
- Every judgement records the licence tier of the text Jev read (input_tier). Anything derived from gray-private
  text is personal use only; report.md says so.
- jevscreen.jev is imported lazily (and the paid client is built through `jev_factory`), so this module and its
  tests work without it. dry_run=True never sends a paid request and writes nothing to the store.
- Layer 2 gates the output: only L2 'explicit' / 'partial' companies are ranked. 'contradicted' is dropped (listed
  as a gap); 'insufficient', failed/uncertain/budget-skipped L2 and passes beyond --l2-max go to a separate
  'unverified' list with their own l2_status. An L2 judgement made from a profile (no annual-report text) is marked
  l2_evidence='profile' and weighs half as much as one made from annual-report excerpts.
- Every step is counted in the funnel, from all universe rows (NULL market cap, below the market-cap / volume
  floors, other countries) to the L1/L2 label mix and the annual-report-vs-profile split of L2 inputs.
- Layer 2 reads official annual-report text from ANY official source in `documents` (SEC 10-K/20-F, CNINFO, EDINET,
  DART, MOPS, BSE; OFFICIAL_DOC_SOURCES). Per company the newest filing wins; a full business section beats a short summary only
  when both are for the same fiscal year and the full text file is present. Excerpt keywords follow the document's
  language (detect_language: zh / ja / ko / en by script); CJK keywords match as plain substrings.
- Automatic translation: a non-English idea (Han / kana / Hangul, or mostly non-Latin letters) is turned into an
  English idea and keywords per language by the LOCAL model (jevscreen.keywords.generate, free, cached) before L1,
  unless translate=False; English --keywords only replace the English excerpt terms. The Jev questions then read
  'idea_en (original: idea)'. If the model is unavailable the old behaviour (verbatim idea, loud
  keyword warning) is kept.
"""
from __future__ import annotations

import contextlib
import csv
import dataclasses
import datetime as dt
import hashlib
import io
import json
import math
import os
import re
import shutil
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

from . import coverage, provenance, store, zhvariants
from . import shells as _shells

# ---------------------------------------------------------------------------------------------------------------
# Parameters and constants

DESC_PRIORITY: tuple[str, ...] = ("tradingview_profile", "financedatabase_local", "sec_filing_text")
DESC_MAX_CHARS = 1800        # combined description sent to L1
DESC_MAX_PARTS = 2           # at most two distinct descriptions are concatenated
EXCERPT_MAX_CHARS = 700      # one L2 excerpt
EXCERPT_MAX_CHARS_CJK = 450  # one L2 excerpt of a zh/ja/ko document (CJK text costs far more tokens per character)
EXCERPT_MAX_N = 3            # excerpts per company
EXCERPT_MIN_PARA = 80        # shorter paragraphs are headings / page footers
EXCERPT_MIN_PARA_CJK = 40    # CJK paragraphs carry more per character
CJK_FRAGMENT_MIN_CHARS = 80  # a shorter CJK keyword excerpt is widened with its neighbouring chunks / paragraphs
WEAK_TERM_WEIGHT = 0.25      # a weak (noisy, high-df) term counts this much when choosing the keyword paragraph
OVERVIEW_MIN_CHARS = 150     # the overview excerpt adds paragraphs until it has this many chars
OVERVIEW_MIN_CHARS_CJK = 300  # same for a zh/ja/ko document (its business sections often open with results)
OUTPUT_EXCERPT_CHARS = 300   # evidence_excerpt in the output table

L1_ADJACENT_MIN = 0.6        # adjacent passes when p_core + p_adjacent >= this
L1_CORE_MIN = 0.0            # core passes when p_core >= this (0: the label alone is enough)
L2_WEIGHTS: dict[str, float] = {"explicit": 3.0, "partial": 1.5, "insufficient": 0.5, "contradicted": -2.0}
L2_VERIFIED = ("explicit", "partial")   # only these are ranked; the rest is 'unverified' or dropped
L2_PROFILE_FACTOR = 0.5      # an explicit/partial label read from a profile (no annual report) weighs half
PROFILE_CAP_ZH = "仅简介"      # a profile-only L2 label is at most partial (never explicit) and is marked so
SEC_MAX_AGE_DAYS = 3 * 365 + 1   # older annual-report text is flagged (l2_doc_stale) and listed as a gap
L1_WEIGHT = 2.0              # score += L1_WEIGHT * p_core
MCAP_TIEBREAK = 0.01         # score += MCAP_TIEBREAK * log10(market cap)
EV_WEIGHTS: dict[str, float] = {"explicit": 3.0, "partial": 1.5}   # score_ev: mean P(label) x weight (--rank ev)
RANKS = ("label", "ev")      # label: score_of (default); ev: score_ev (measured in the live check, not the default)

# Repeated L2 reads (stability): items whose read-0 p = P(explicit) + P(partial) lies in L2_BAND get reads 1..K-1
# (Question.read salts the cache key, packets reshuffled per read) and are decided on the mean of their ok reads.
L2_READS_DEFAULT = 3         # --reads K; 1 reproduces the single-read behaviour exactly
L2_BAND = (0.30, 0.75)       # read-0 p_pos range that gets the extra reads (inclusive)
L2_EDGE = (0.40, 0.60)       # 边缘: l2_edge when L2_EDGE[0] <= mean p_pos < L2_EDGE[1] after all reads
BAND_SHARE_ESTIMATE = 0.28   # dry-run estimate: share of L2 items in the band (measured on the first live runs)
READ_ONCE_NOTE = "只读了一次"   # a band item whose extra reads were skipped (budget / provider)

JEV_DEFAULT_RATE, JEV_DEFAULT_WORKERS = 15.0, 16
JEV_ASSUMED_LATENCY_S = 4.0  # used only for the time estimate in reports

TIER_ORDER = {"official-open": 0, "official-private": 1, "gray-private": 2}

STATUS_OK, STATUS_PARTIAL, STATUS_BUDGET, STATUS_UNAVAILABLE, STATUS_DRY, STATUS_BUSY = (
    "ok", "partial", "budget_exhausted", "jev_unavailable", "dry_run", "jev_busy")

READ_WAIT_S, WRITE_WAIT_S = 120.0, 600.0

L1_KEY, L2_KEY = "fit", "evidence"

L1_INSTRUCTIONS = (
    'Judge ONLY from state.text how state.issuer\'s CURRENT business relates to the investment idea "{idea}". '
    "Do not use outside knowledge about the company. The company name alone is not evidence. "
    "Former businesses that the text says were sold or discontinued do not count.")
L1_CRITERIA: dict[str, str] = {
    "core": "A main product or service of the company directly implements, sells or supplies what the idea is about.",
    "adjacent": "Some product line is related to the idea or is a clear enabler of it, but it is not central to the "
                "company's business.",
    "unrelated": "The description shows businesses that are unrelated to the idea.",
    "insufficient": "The description is too vague or too short to tell.",
}
L2_INSTRUCTIONS = (
    "state.text is evidence about state.issuer: either excerpts from its latest annual report (it starts with "
    '"[annual report excerpts") or, when no annual report text is available, a short company profile (it starts with '
    '"[company profile"). Using ONLY state.text, decide whether it EXPLICITLY states that the company offers or '
    'operates a product or service that directly implements the investment idea "{idea}". Do not use outside '
    "knowledge. The company name alone is not evidence. Excerpts that simply do not mention the idea are not a "
    "contradiction: they are insufficient.")
L2_CRITERIA: dict[str, str] = {
    "explicit": "The text explicitly states that the company offers or operates a product or service that directly "
                "implements the idea.",
    "partial": "The text describes a related product, a small or planned activity, or an enabler, but not an explicit "
               "offering that directly implements the idea.",
    "contradicted": "The text explicitly says that the related activity was sold, discontinued or wound down, or that "
                    "the company does not offer it.",
    "insufficient": "The text does not say enough to decide, including when it only describes other businesses of the "
                    "company.",
}

_STOP = frozenset("""
a an and are as at be been being but by can could company companies corporation inc ltd plc co group holdings
holding for from has have its it in into is of on or our such that the their them these they this those through to
under was were which while will with within also other including include includes provides provide products product
services service business businesses segment segments operations operates operate other various well based
related worldwide global including world market markets customers customer headquartered founded incorporated
limited solutions offers offer primarily principally engaged engages through subsidiaries subsidiary
""".split())

CSV_COLUMNS: tuple[str, ...] = (
    "rank", "security_id", "company_key", "name", "country", "region", "market_cap_usd", "revenue_ttm_usd",
    "revenue_cagr_3y_local", "cagr_currency", "cagr_years", "l1_label", "l1_p_core", "l2_label", "l2_p_top",
    "l2_status", "l2_evidence", "score", "evidence_excerpt", "evidence_url", "filing_source", "filing_form",
    "filing_date", "doc_lang", "l2_doc_stale", "l2_keyword_hit", "l1_input_tier", "l2_input_tier", "l2_input_source",
    "l1_request_id", "l2_request_id", "l2_reads", "l2_p_pos", "l2_p_pos_sd", "l2_edge", "user_verdict",
    "verdict_source", "backfill", "flags", "doc_fetch")   # flags: shells marks (st / star_st / spac_like), ';'-joined

# Official annual-report sources whose `documents` text layer 2 reads (source_id -> short label for the L2 tag and
# the default language when the text is too short to detect).
OFFICIAL_DOC_SOURCES: dict[str, tuple[str, str]] = {
    "sec_filing_text": ("SEC", "en"),
    "cninfo_annual_report": ("CNINFO", "zh"),
    "edinet_yuho": ("EDINET", "ja"),
    "dart_business_report": ("DART", "ko"),
    "mops_annual_report": ("MOPS", "zh"),
    "bse_annual_report": ("BSE", "en"),
}
# identifiers.id_type -> documents.source_id reached through documents.cik (SEC class lines share one CIK).
NATIVE_ID_TYPES: dict[str, str] = {"sec_cik": "sec_filing_text", "cninfo_orgid": "cninfo_annual_report",
                                   "edinet_code": "edinet_yuho", "dart_corp_code": "dart_business_report",
                                   "mops_co_id": "mops_annual_report", "bse_scrip_code": "bse_annual_report"}
LANGS: tuple[str, ...] = ("en", "zh", "ja", "ko")
CJK_LANGS = frozenset({"zh", "ja", "ko"})


# ---------------------------------------------------------------------------------------------------------------
# Jev contract glue (lazy)

@dataclasses.dataclass
class _FallbackQuestion:          # mirrors jev.Question when jevscreen.jev is not importable
    key: str
    instructions: str
    criteria: dict[str, str]
    read: int = 0                 # repeat-read index (salts jev.item_key when > 0)


@dataclasses.dataclass
class _FallbackItem:              # mirrors jev.Item
    item_id: str
    issuer: str
    text: str
    meta: dict = dataclasses.field(default_factory=dict)


def _jev_types() -> tuple[type, type]:
    try:
        from . import jev
        return jev.Question, jev.Item
    except ImportError:
        return _FallbackQuestion, _FallbackItem


def _default_factory(cfg, *, run_id: str, layer: str, budget_usd: float, dry_run: bool,
                     retry_uncertain: bool = False, provider=None):
    from . import jev
    return jev.JevClient(cfg, run_id=run_id, layer=layer, budget_usd=budget_usd, dry_run=dry_run,
                         retry_uncertain=retry_uncertain, provider=provider)


_REAL_DEFAULT_FACTORY = _default_factory


def _run_factory(cfg):
    """The real client factory of one screen run: the Jev provider is resolved once here, so layer 1 and layer 2
    go through the same provider even if a key is set or cleared while the run is going (an unknown
    JEVSCREEN_JEV_PROVIDER is left to JevClient, which reports it before anything is sent). A replaced
    _default_factory (a test fake) is used as it is."""
    from . import jev
    if _default_factory is not _REAL_DEFAULT_FACTORY:
        return _default_factory
    try:
        provider = jev.resolve_provider(cfg)[0]
    except jev.ProviderError:
        provider = None

    def factory(cfg, **kw):
        return _default_factory(cfg, provider=provider, **kw)
    factory.provider = provider
    return factory


def _is_error(e: BaseException, name: str) -> bool:
    """True if e is (a subclass of) an exception class called `name` (jev.JevUnavailable / jev.BudgetExceeded).
    Matched by name so a fake client in tests and the real module behave the same."""
    return any(c.__name__ == name for c in type(e).__mro__)


def question_idea(idea: str, idea_en: str | None = None) -> str:
    """The idea text embedded in the Jev questions: the verbatim idea, or 'idea_en (original: idea)' when an English
    rendering exists (English costs far fewer tokens per character and reads unambiguously for the model)."""
    idea = idea.strip()
    en = (idea_en or "").strip()
    if not en or en == idea:
        return idea
    return f"{en} (original: {idea})"


def build_l1_question(idea: str, idea_en: str | None = None):
    Question, _ = _jev_types()
    return Question(key=L1_KEY, instructions=L1_INSTRUCTIONS.format(idea=question_idea(idea, idea_en)),
                    criteria=dict(L1_CRITERIA))


def build_l2_question(idea: str, idea_en: str | None = None, rules: Iterable[Any] = (),
                      facets: dict[str, str] | None = None):
    """The L2 question. `rules` (library ids or sieve rule entries, rendered with the sieve's `facets` by
    calib.render_rules) append their sentences to their criterion, sorted by rule id and deduplicated. No rules gives
    exactly the question (and so the item keys) of before calibration existed; the same rule set always gives the
    same question."""
    Question, _ = _jev_types()
    criteria = dict(L2_CRITERIA)
    rules = list(rules or ())
    if rules:
        from . import calib
        extra: dict[str, list[str]] = {}
        for _rule_id, label, text in calib.render_rules(rules, facets):
            if label in criteria and text not in extra.setdefault(label, []):
                extra[label].append(text)
        criteria = {label: " ".join([base] + extra.get(label, [])) for label, base in criteria.items()}
    return Question(key=L2_KEY, instructions=L2_INSTRUCTIONS.format(idea=question_idea(idea, idea_en)),
                    criteria=criteria)


def _question_dict(q) -> dict[str, Any]:
    """A question for results.json: its fields, without `read` when it is 0 (as before repeated reads existed)."""
    d = dataclasses.asdict(q)
    if not d.get("read"):
        d.pop("read", None)
    return d


# ---------------------------------------------------------------------------------------------------------------
# Text helpers (pure, deterministic)

# A sentence end: CJK full stops (with closing brackets), or ASCII ones followed by whitespace (Korean, English).
_SENT_END = re.compile(r"[。！？][」』）)]*|[.!?](?=\s)")
# The token before an ASCII full stop that makes it an enumerator (1. / d. / 가. / iv.), not a sentence end.
_ENUM_TOKEN = re.compile(r"(?:\d{1,3}|[A-Za-z]|[ivxIVX]{1,4}|[가나다라마바사아자차카타파하])\.")


def _is_enumerator(text: str, dot_end: int) -> bool:
    """True when the '.' ending at dot_end closes an enumerator token (preceded by whitespace or the text start)."""
    sp = max(text.rfind(" ", 0, dot_end), text.rfind("\n", 0, dot_end), text.rfind("　", 0, dot_end))
    return bool(_ENUM_TOKEN.fullmatch(text, sp + 1, dot_end))


def _sent_ends(text: str, pos: int = 0, endpos: int | None = None) -> list[int]:
    """Sentence-end offsets (_SENT_END) in text[pos:endpos], enumerators (1. / 가.) excluded."""
    endpos = len(text) if endpos is None else endpos
    return [m.end() for m in _SENT_END.finditer(text, pos, endpos)
            if not (m.group() in ".!?" and _is_enumerator(text, m.end()))]


def truncate(text: str | None, n: int, lang: str | None = None) -> str | None:
    """Cut to at most n chars (ellipsis included). None stays None. English cuts at a word boundary. A zh/ja/ko
    text (`lang`) cuts at the last sentence end past 60% of n; failing that Korean cuts at a space and Chinese /
    Japanese hard-cut: their only spaces sit inside Latin names or where flattened paragraphs were joined."""
    if text is None:
        return None
    text = re.sub(r"[ \t]+", " ", text).strip()
    if len(text) <= n:
        return text
    cut = text[: n - 1]
    if lang in CJK_LANGS:
        ends = _sent_ends(cut)
        if ends and ends[-1] > n * 0.6:
            return cut[: ends[-1]] + "…"
        if lang != "ko":
            return cut.rstrip() + "…"
    sp = cut.rfind(" ")
    if sp > n * 0.6:
        cut = cut[:sp]
    return cut.rstrip(" ,;:") + "…"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def most_restrictive_tier(tiers: Iterable[str | None]) -> str | None:
    tiers = [t for t in tiers if t]
    return max(tiers, key=lambda t: TIER_ORDER.get(t, 3)) if tiers else None


def _tier(source_id: str) -> str | None:
    try:
        return provenance.tier_of(source_id).value
    except KeyError:
        return None


def select_description(descs: list[tuple[str, str, bool]]) -> dict[str, Any] | None:
    """Pick the text Jev layer 1 reads for one company.

    descs: (source_id, text, on_own_line). Per source the text on the company's own universe line wins, else the
    longest. Sources are ranked tradingview_profile > financedatabase_local > sec_filing_text (> others), and at most
    DESC_MAX_PARTS distinct texts are joined with source tags, up to DESC_MAX_CHARS. None when nothing usable."""
    best: dict[str, tuple[bool, int, str]] = {}
    for source_id, text, own in descs:
        if not text or not text.strip():
            continue
        cand = (bool(own), len(text), text.strip())
        if source_id not in best or cand[:2] > best[source_id][:2]:
            best[source_id] = cand
    if not best:
        return None
    order = sorted(best, key=lambda s: (DESC_PRIORITY.index(s) if s in DESC_PRIORITY else len(DESC_PRIORITY), s))
    parts: list[tuple[str, str]] = []
    for s in order:
        t = best[s][2]
        n = _norm(t)
        if any(n == _norm(p) or n in _norm(p) or _norm(p) in n for _, p in parts):
            continue
        parts.append((s, t))
        if len(parts) >= DESC_MAX_PARTS:
            break
    if len(parts) == 1:
        text = f"[{parts[0][0]}] {parts[0][1]}"
    else:
        first = f"[{parts[0][0]}] {parts[0][1]}"
        if len(first) > DESC_MAX_CHARS - 300:       # leave the second source some room
            first = truncate(first, DESC_MAX_CHARS - 300)
        text = f"{first}\n\n[{parts[1][0]}] {parts[1][1]}"
    text = truncate(text, DESC_MAX_CHARS) if len(text) > DESC_MAX_CHARS else text
    sources = [s for s, _ in parts]
    return {"text": text, "sources": sources, "tier": most_restrictive_tier(_tier(s) for s in sources),
            "plain": " ".join(t for _, t in parts)}


# EDINET yuho boilerplate at the head of 経営方針 (and elsewhere): says nothing about the business. Its variants
# (文中の / 文中における / 本項における ...) go together with a leading なお、 / また、.
_JA_DISCLAIMER = re.compile(r"(?:なお、|また、)?(?:本文中|文中|本項|以下の記載)(?:の|における|に記載した|に記載されている)?"
                            r"将来に関する事項は[^。]{0,120}。")
# Item markers that open a new paragraph in a flattened CJK block: （１） (1) ① i. ⅰ Ⅰ 【…】, Korean 가. / 1.
_ITEM_MARKER = re.compile(r"(?:\s+|(?<=[。！？]))(?=（[0-9０-９]{1,2}）|\([0-9]{1,2}\)|[①-⑳]|[ⅰ-ⅻⅠ-Ⅻ]"
                          r"|(?:i{1,3}|iv|vi{0,3}|ix|x)\.\s|【|[가나다라마바사아자차카타파하]\.\s|\d{1,2}\.\s+[가-힣])")
# Where a flattened CJK paragraph may be cut: after a sentence end (CJK, or ASCII before non-ASCII text: Korean)
_CJK_SENT_SPLIT = re.compile(r"(?<=[。！？])(?:[」』）)]*)\s*|(?<=[.!?])\s+(?=[^\x00-\x7f])")
_QUOTE_PAIRS = {"「": "」", "『": "』", "（": "）"}
CJK_SPLIT_CHARS = 600         # a CJK paragraph longer than this is split into sentence chunks (split_paragraphs)


def _quoted_spans(text: str, limit: int) -> list[tuple[int, int]]:
    """(opener, closer) offsets of the 「…」 / 『…』 / （…） pairs at most `limit` chars long. An unclosed opener
    spans nothing, so a stray bracket never blocks the rest of a paragraph."""
    closers = {v: k for k, v in _QUOTE_PAIRS.items()}
    stack: list[tuple[str, int]] = []
    spans = []
    for i, ch in enumerate(text):
        if ch in _QUOTE_PAIRS:
            stack.append((ch, i))
        elif ch in closers:
            for k in range(len(stack) - 1, -1, -1):
                if stack[k][0] == closers[ch]:
                    if i - stack[k][1] <= limit:
                        spans.append((stack[k][1], i))
                    del stack[k:]
                    break
    return spans


def _split_long_cjk(para: str, max_chars: int, min_len: int) -> list[str]:
    """Split one long CJK paragraph into chunks of whole sentences, each <= max_chars. An item marker (（１）, i.,
    ①, 가. ...) always starts a chunk; a chunk shorter than min_len (a heading) is carried into the next one unless
    that would pass max_chars (it then stands alone, and split_paragraphs drops it); a single sentence longer than
    max_chars is cut at a 、 / space near max_chars. Nothing is cut inside a quote or bracket (「…」 『…』 （…）),
    and an ASCII full stop after an enumerator (d. / 1. / 가.) is not a sentence end."""
    depth = [0] * (len(para) + 2)                # depth[p] > 0: offset p lies inside a quote (o < p <= c)
    for o, c in _quoted_spans(para, max_chars):
        depth[o + 1] += 1
        depth[c + 1] -= 1
    for i in range(1, len(depth)):
        depth[i] += depth[i - 1]

    def inside(p: int) -> bool:
        return depth[p] > 0

    cuts: dict[int, bool] = {}                   # offset -> starts an item
    for m in _CJK_SENT_SPLIT.finditer(para):
        p = m.end()
        if 0 < p < len(para) and not inside(p) and not (para[m.start() - 1] in ".!?"
                                                         and _is_enumerator(para, m.start())):
            cuts[p] = False
    for m in _ITEM_MARKER.finditer(para):
        if 0 < m.end() < len(para) and not inside(m.end()):
            cuts[m.end()] = True
    segs: list[tuple[bool, str]] = []            # (starts an item, text)
    bounds = sorted(cuts) + [len(para)]
    pos = 0
    for end in bounds:
        piece, first = para[pos:end].strip(), cuts.get(pos, False)
        pos = end
        while len(piece) > max_chars:
            cut = max(piece.rfind("、", 0, max_chars), piece.rfind(" ", 0, max_chars))
            cut = cut + 1 if cut > max_chars * 0.6 else max_chars
            segs.append((first, piece[:cut].strip()))
            piece, first = piece[cut:].strip(), False
        if piece:
            segs.append((first, piece))
    out: list[str] = []
    cur = ""
    for starts_item, seg in segs:
        over = len(cur) + 1 + len(seg) > max_chars
        if cur and (over or (starts_item and len(cur) >= min_len)):
            out.append(cur)
            cur = ""
        cur = f"{cur} {seg}".strip() if cur and not re.search(r"[。！？」』）)]$", cur) else cur + seg
    if cur:
        out.append(cur)
    return out


def split_paragraphs(text: str, min_len: int = EXCERPT_MIN_PARA, split_long: int | None = None) -> list[str]:
    """Blank-line paragraphs (single lines when a document has few blank lines); headings/footers dropped.

    split_long (CJK documents, the excerpt cap): paragraphs longer than CJK_SPLIT_CHARS are split into chunks of
    whole sentences of at most split_long chars, starting a chunk at each item marker, and the yuho forward-looking
    disclaimer is dropped. EDINET XBRL text blocks arrive flattened (one line per block, the original paragraph
    breaks surviving as single spaces), so without this a whole 事業の内容 was one "paragraph" and excerpts were
    arbitrary 450-char windows of it."""
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paras) < 4:
        paras = [p for p in text.split("\n") if p.strip()]
    out = []
    for p in paras:
        p = re.sub(r"\s+", " ", p).strip()
        if split_long:
            p = re.sub(r"\s+", " ", _JA_DISCLAIMER.sub(" ", p)).strip()
        if split_long and len(p) > max(CJK_SPLIT_CHARS, split_long):
            out.extend(c for c in _split_long_cjk(p, split_long, min_len) if len(c) >= min_len)
        elif len(p) >= min_len:
            out.append(p)
    return out


_HAN = r"㐀-䶿一-鿿豈-﫿"
_KANA = r"぀-ヿㇰ-ㇿｦ-ﾟ"
_HANGUL = r"ᄀ-ᇿ㄰-㆏가-힯"
_RE_HAN, _RE_KANA, _RE_HANGUL = re.compile(f"[{_HAN}]"), re.compile(f"[{_KANA}]"), re.compile(f"[{_HANGUL}]")
_RE_LATIN = re.compile(r"[A-Za-z]")
LANG_SAMPLE_CHARS = 20_000   # detect_language looks at the start of the text only
LANG_MIN_SCRIPT_CHARS = 20   # fewer script characters than this -> the caller's default


def script_counts(text: str) -> dict[str, int]:
    return {"han": len(_RE_HAN.findall(text)), "kana": len(_RE_KANA.findall(text)),
            "hangul": len(_RE_HANGUL.findall(text)), "latin": len(_RE_LATIN.findall(text))}


def detect_language(text: str | None, default: str | None = "en") -> str | None:
    """'zh' | 'ja' | 'ko' | 'en' by script (first LANG_SAMPLE_CHARS characters), else `default`.

    Hangul dominates -> ko; kana make up >= 10% of the Han + kana characters -> ja (Japanese prose is ~30-60% kana);
    Han characters outnumber a fifth of the Latin letters -> zh; mostly Latin letters -> en. Text with fewer than
    LANG_MIN_SCRIPT_CHARS letters of any of these scripts returns `default` (e.g. the source's own language)."""
    if not text:
        return default
    c = script_counts(text[:LANG_SAMPLE_CHARS])
    cjk = c["han"] + c["kana"] + c["hangul"]
    if cjk + c["latin"] < LANG_MIN_SCRIPT_CHARS:
        return default
    if c["hangul"] >= max(LANG_MIN_SCRIPT_CHARS // 2, 0.3 * cjk) and c["hangul"] * 5 >= c["latin"]:
        return "ko"
    if c["han"] + c["kana"] >= LANG_MIN_SCRIPT_CHARS // 2 and (c["han"] + c["kana"]) * 5 >= c["latin"]:
        if c["kana"] >= 0.1 * (c["han"] + c["kana"]):
            return "ja"
        return "zh"
    if c["latin"] >= LANG_MIN_SCRIPT_CHARS:
        return "en"
    return default


def has_non_ascii(text: str | None) -> bool:
    return bool(text) and re.search(r"[^\x00-\x7f]", text) is not None


def needs_translation(text: str | None) -> bool:
    """True when the idea is not English: it has Han / kana / Hangul characters, or Latin letters are under half of
    its letters (e.g. Cyrillic). Typographic quotes, dashes and accented Latin letters ('AI agents’ identity',
    'Nestlé suppliers') do not count, so an English idea never loads the local model nor changes the Jev question."""
    if not text:
        return False
    t = unicodedata.normalize("NFKC", text)
    c = script_counts(t)
    if c["han"] + c["kana"] + c["hangul"]:
        return True
    letters = [ch for ch in t if ch.isalpha()]
    if not letters:
        return False
    latin = sum(1 for ch in letters if "LATIN" in unicodedata.name(ch, ""))
    return latin * 2 < len(letters)


_ZH_SPLIT = re.compile(r"[的与和及或、，,。；;：:（）()《》“”\"'\s]+")


def cjk_idea_terms(idea: str, lang: str) -> list[str]:
    """Fallback excerpt terms for a zh/ja/ko document taken from the idea itself, only when the idea is written in
    that language's script: Han runs for zh (no kana in the idea), Han/kana runs for ja (the idea has kana), Hangul
    runs for ko; split at particles and punctuation, runs of >= 2 characters, longest first."""
    if not idea:
        return []
    c = script_counts(idea)
    if lang == "ko":
        runs = re.findall(f"[{_HANGUL}]{{2,}}", idea)
    elif lang == "ja":
        if not c["kana"]:
            return []
        runs = [r for part in re.split(r"[のとや、，。\s]+", idea) for r in re.findall(f"[{_HAN}{_KANA}]{{2,}}", part)]
    elif lang == "zh":
        if c["kana"] or not c["han"]:
            return []
        runs = [r for part in _ZH_SPLIT.split(idea) for r in re.findall(f"[{_HAN}]{{2,}}", part)]
    else:
        return []
    seen, out = set(), []
    for r in sorted(runs, key=len, reverse=True):
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


_ACRONYM = re.compile(r"(?=[A-Z0-9]*[A-Z])[A-Z0-9]{2,5}")     # AI, EV, 5G, 3D, LLM, IAM


def _usable_word(w: str) -> bool:
    return bool(_ACRONYM.fullmatch(w)) or (len(w) >= 3 and w[0].isalpha() and w.lower() not in _STOP)


def idea_terms(idea: str, keywords: list[str] | None) -> list[str]:
    """Search terms for the keyword excerpt: the user's keywords (phrases allowed), else from the Latin part of the
    idea: runs of 2+ adjacent usable words as phrases ('AI agent'), then the single words (>= 3 letters, stopwords
    dropped; all-caps acronyms such as AI, EV, 5G kept). A Chinese idea without --keywords yields few or none."""
    if keywords:
        return [k.strip() for k in keywords if k and k.strip()]
    tokens = [(m.group(0), m.start(), m.end()) for m in re.finditer(r"[A-Za-z0-9][A-Za-z0-9\-]*", idea)]
    phrases, run = [], []
    for i, (w, st, _en) in enumerate(tokens):
        adjacent = bool(run) and re.fullmatch(r"[ \t]+", idea[tokens[i - 1][2]:st] or "x") is not None
        if _usable_word(w) and (not run or adjacent):
            run.append(w)
            continue
        if len(run) >= 2:
            phrases.append(" ".join(run[:4]))
        run = [w] if _usable_word(w) else []
    if len(run) >= 2:
        phrases.append(" ".join(run[:4]))
    seen, out = set(), []
    for t in phrases + [w for w, _, _ in tokens if _usable_word(w)]:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def keyword_warnings(idea: str, keywords: list[str] | None, terms: list[str], *, translated: bool = False
                     ) -> list[str]:
    """Loud warnings when the English keyword excerpts will be poor: no --keywords, no automatic translation and a
    non-English idea."""
    if keywords or translated:
        return []
    if not terms:
        return ["No keyword terms: the idea has no usable Latin words, so layer 2 reads only the opening and the "
                "description-overlap paragraphs of each annual report. Pass --keywords with English terms "
                "(phrases allowed, e.g. 'AI agent,access management').", ]
    if needs_translation(idea):
        return [f"The idea is not only in English: SEC keyword excerpts use just {terms}. Pass --keywords with "
                "English terms and phrases for the idea (e.g. 'AI agent,identity,access management,authorization') "
                "so layer 2 reads the relevant passages."]
    return []


_SUFFIX = r"(?:s|es|ic|ics|al|ed|ing)?"


_ALNUM_BOUNDARY = r"A-Za-z0-9Ａ-Ｚａ-ｚ０-９"


def _ch(ch: str) -> str:
    """Regex for one (NFKC-normalised) term character: printable ASCII also matches its full-width form (ＡＩ, ＩＤ,
    ２, Ｍ＆Ａ, （）, ／), which Japanese / Chinese filings use and which NFKC folds away from the term; a Chinese
    character also matches its Simplified / Traditional variants (zhvariants: 认 認)."""
    if "!" <= ch <= "~":
        return f"[{re.escape(ch)}{re.escape(chr(ord(ch) + 0xFEE0))}]"
    forms = zhvariants.variants(ch)           # Simplified <-> Traditional (MOPS filings are Traditional)
    return f"[{forms}]" if len(forms) > 1 else re.escape(ch)


def _term_pattern(term: str) -> re.Pattern:
    """Whole-word (or whole-phrase) match for ASCII terms, with simple inflections on the last word (agent ->
    agents / agentic, robot -> robots / robotic). All-caps acronyms (AI, EV) match case-sensitively, plural only.
    Other terms (Chinese / Japanese / Korean, possibly mixed with Latin: 'AI 代理权限控制') match as substrings with
    their spaces made optional and optional whitespace allowed between characters: CJK documents are written without
    the spaces a model puts between scripts (cninfo.normalise_line removes them), and Korean spacing varies.
    Terms are NFKC-normalised and their ASCII letters / digits also match the full-width forms (ＩＤ管理, ＡＰＩ)."""
    t = unicodedata.normalize("NFKC", term).strip()
    b = _ALNUM_BOUNDARY
    if re.fullmatch(r"[\w\- .]+", t, flags=re.ASCII):
        words = t.split()
        body = r"\s+".join("".join(_ch(c) for c in w) for w in words)
        if len(words) == 1 and _ACRONYM.fullmatch(t):
            return re.compile(rf"(?<![{b}])" + body + rf"[sｓ]?(?![{b}])")
        suffix = _SUFFIX if len(words[-1]) >= 3 else "s?"
        return re.compile(rf"(?<![{b}])" + body + suffix + rf"(?![{b}])", re.IGNORECASE)
    chars = [ch for ch in t if not ch.isspace()]
    if not chars:
        return re.compile(r"(?!x)x")
    body = r"\s*".join(_ch(ch) for ch in chars)
    if re.match(r"[A-Za-z0-9]", chars[0]):
        body = rf"(?<![{b}])" + body
    if re.match(r"[A-Za-z0-9]", chars[-1]):
        body += rf"(?![{b}])"
    return re.compile(body, re.IGNORECASE)


# Results / financial-figure vocabulary: a CJK paragraph with these and many digits is a results summary, not a
# business description (Chinese 主要业务 sections usually open with revenue and profit figures).
_FIGURE_WORDS = ("营业收入", "营业总收入", "净利润", "同比", "较去年同期", "较上年", "比上年", "万元", "亿元",
                 "売上高", "売上収益", "営業利益", "経常利益", "純利益", "百万円", "億円", "前期比", "前年同期比",
                 "매출액", "영업이익", "순이익", "당기순이익", "억원", "백만원", "전년 대비", "전년대비")


def figure_heavy(para: str) -> bool:
    """A paragraph dominated by financial figures: >= 12% digits, or >= 5% digits with >= 2 results words."""
    if not para:
        return False
    digits = sum(ch.isdigit() for ch in para)
    ratio = digits / len(para)
    if ratio >= 0.12:
        return True
    return ratio >= 0.05 and sum(1 for w in _FIGURE_WORDS if w in para) >= 2


def _description_terms(description: str | None) -> set[str]:
    if not description:
        return set()
    return {w for w in re.findall(r"[a-z][a-z\-]{5,}", description.lower()) if w not in _STOP}


def _keyword_window(para: str, pats: list[re.Pattern], max_chars: int, lang: str | None) -> str:
    """The keyword excerpt of one paragraph: the whole paragraph when it fits, else the max_chars window with the
    most distinct terms (then most hits; ties -> earliest, so the paragraph opening stays when it does as well),
    each candidate opening max_chars // 3 before one of the hits. The start moves back to a word boundary (English;
    the paragraph start when the window opens inside the first word) or to a sentence end at most max_chars // 6
    back (zh / ja / ko: their spaces sit inside Latin names or where flattened paragraphs were joined, often
    hundreds of characters earlier). Candidates are scored on the text actually emitted (after that move and the
    truncate cut), and the excerpt always contains a hit: if every cut loses it, the window restarts at the hit."""
    if len(para) <= max_chars:
        return para
    hits = sorted((m.start(), i) for i, p in enumerate(pats) for m in p.finditer(para))
    if not hits:
        return truncate(para, max_chars, lang)
    lead = max_chars // 3

    def emit(start: int) -> str:
        if start:
            if lang in CJK_LANGS:
                ends = _sent_ends(para, max(0, start - max_chars // 6), start)
                if ends:
                    start = ends[-1]
                elif lang == "ko":
                    sp = para.rfind(" ", max(0, start - max_chars // 6), start)
                    start = sp + 1 if sp >= 0 else start
            else:
                sp = para.rfind(" ", 0, start)
                start = sp + 1 if sp >= 0 else 0
        return truncate(("…" if start else "") + para[start:].lstrip(), max_chars, lang)

    best: tuple[tuple[int, int], str] | None = None
    for start in dict.fromkeys(max(0, a - lead) for a, _ in hits):
        text = emit(start)
        found = [i for i, p in enumerate(pats) for _ in p.finditer(text)]
        key = (len(set(found)), len(found))
        if best is None or key > best[0]:
            best = (key, text)
    if best[0][0]:
        return best[1]
    start = max(0, hits[0][0] - 20)
    return truncate(("…" if start else "") + para[start:], max_chars, lang)


def _widen_fragment(paras: list[str], j: int, used: set[int], pats: list[re.Pattern], max_chars: int,
                    lang: str | None, skip: set[int] = frozenset()) -> str:
    """A short CJK keyword chunk paras[j] joined with its unused neighbours (the chunks of the same flattened
    paragraph, or the neighbouring paragraphs; one before and one after per round, never across a used or `skip`
    (figure-heavy) paragraph) until the joined text reaches max_chars or nothing is left, then cut to max_chars
    around its hits (_keyword_window). The neighbours taken are marked used, so no later excerpt repeats them."""
    lo = hi = j
    free = lambda k: 0 <= k < len(paras) and k not in used and k not in skip  # noqa: E731
    while len(" ".join(paras[lo:hi + 1])) < max_chars and (free(lo - 1) or free(hi + 1)):
        if free(lo - 1):
            lo -= 1
        if free(hi + 1) and len(" ".join(paras[lo:hi + 1])) < max_chars:
            hi += 1
    used.update(range(lo, hi + 1))
    return _keyword_window(" ".join(paras[lo:hi + 1]), pats, max_chars, lang)


def build_excerpts(text: str, *, terms: list[str] | None = None, description: str | None = None,
                   max_n: int = EXCERPT_MAX_N, max_chars: int | None = None, lang: str | None = None,
                   weak_terms: Iterable[str] = ()) -> list[dict[str, str]]:
    """Up to max_n excerpts (<= max_chars each) of an annual-report section, deterministic:
    1. 'overview': the opening paragraph (the next ones too while shorter than OVERVIEW_MIN_CHARS);
    2. 'keywords': the paragraph with the most distinct term hits (case-insensitive; ties -> earliest);
    3. 'description': the paragraph sharing the most distinctive words (>= 6 letters) with the company's own
       description (at least 2), ties -> earliest.
    Paragraphs are never used twice. A zh/ja/ko document (`lang`) uses the shorter CJK caps (EXCERPT_MAX_CHARS_CJK,
    EXCERPT_MIN_PARA_CJK); its keywords match as substrings (no word boundaries in CJK text). For CJK the overview
    skips paragraphs dominated by financial figures (figure_heavy) and runs to OVERVIEW_MIN_CHARS_CJK, and slots
    left free by 'keywords' / 'description' are filled ('context') with the next unused non-figure paragraphs, so a
    section that opens with results still yields business text.
    weak_terms (noisy seeds from the sieve, e.g. 認証) count WEAK_TERM_WEIGHT each and a normal term 1 when the
    keyword paragraph is chosen; a paragraph scoring below 1 is never the keyword paragraph (so weak-only hits give
    no keyword excerpt), but weak terms still break ties. A CJK keyword excerpt shorter than CJK_FRAGMENT_MIN_CHARS
    (a lone sentence chunk of a flattened block) is widened with the unused chunk / paragraph before and after it,
    up to max_chars, around its hits."""
    cjk = lang in CJK_LANGS
    if max_chars is None:
        max_chars = EXCERPT_MAX_CHARS_CJK if cjk else EXCERPT_MAX_CHARS
    tlang = lang if cjk else None
    paras = split_paragraphs(text, EXCERPT_MIN_PARA_CJK if cjk else EXCERPT_MIN_PARA,
                             split_long=max_chars if cjk else None)
    if not paras:
        flat = re.sub(r"\s+", " ", text).strip()
        return [{"kind": "overview", "text": truncate(flat, max_chars, tlang)}] if flat else []
    used: set[int] = set()
    out: list[dict[str, str]] = []
    overview_min = OVERVIEW_MIN_CHARS_CJK if cjk else OVERVIEW_MIN_CHARS
    heavy = {j for j, p in enumerate(paras) if figure_heavy(p)} if cjk else set()
    if len(heavy) == len(paras):
        heavy = set()   # nothing but figures: read them rather than nothing
    weak = {unicodedata.normalize("NFKC", t).strip().lower() for t in weak_terms or () if t and t.strip()}
    pats = [_term_pattern(t) for t in (terms or []) if unicodedata.normalize("NFKC", t).strip().lower() not in weak]
    wpats = [_term_pattern(t) for t in sorted(weak)]

    def best_keyword_para(exclude: set[int]) -> tuple[float, int]:
        best = max(((sum(1 for p in pats if p.search(para)) + WEAK_TERM_WEIGHT * sum(1 for p in wpats
                                                                                     if p.search(para)), -j)
                    for j, para in enumerate(paras) if j not in exclude), default=(0, 0))
        return best if best[0] >= 1 else (0, 0)

    # CJK: the longer overview must not swallow the keyword paragraph, so it is reserved first
    reserved = set()
    if cjk and pats and max_n > 1:
        hit = best_keyword_para(set())
        if hit[0] > 0:
            reserved.add(-hit[1])
    buf: list[str] = []
    for j, para in enumerate(paras):
        if buf and len(" ".join(buf)) >= overview_min:
            break
        if j in heavy or j in reserved:
            continue
        buf.append(para)
        used.add(j)
    if not buf and reserved:     # the keyword paragraph is the only usable one
        reserved.clear()
    if buf:
        out.append({"kind": "overview", "text": truncate(" ".join(buf), max_chars, tlang)})

    if pats and len(out) < max_n:
        best = best_keyword_para(used)
        if best[0] > 0:
            j = -best[1]
            used.add(j)
            kw_text = _keyword_window(paras[j], pats + wpats, max_chars, tlang)
            if cjk and len(kw_text) < CJK_FRAGMENT_MIN_CHARS:
                kw_text = _widen_fragment(paras, j, used, pats + wpats, max_chars, tlang, heavy)
            out.append({"kind": "keywords", "text": kw_text})

    dterms = _description_terms(description)
    if dterms and len(out) < max_n:
        best = max(((len(dterms & set(re.findall(r"[a-z][a-z\-]{5,}", para.lower()))), -j)
                    for j, para in enumerate(paras) if j not in used), default=(0, 0))
        if best[0] >= 2:
            j = -best[1]
            used.add(j)
            out.append({"kind": "description", "text": truncate(paras[j], max_chars, tlang)})

    if cjk:
        j = 0
        while len(out) < max_n:
            buf = []
            while j < len(paras) and (not buf or len(" ".join(buf)) < overview_min):
                j += 1
                if j - 1 in used or j - 1 in heavy:
                    if buf:
                        break   # keep one context excerpt contiguous
                    continue
                buf.append(paras[j - 1])
                used.add(j - 1)
            if not buf:
                break
            out.append({"kind": "context", "text": truncate(" ".join(buf), max_chars, tlang)})
    return out[:max_n]


def revenue_cagr_local(rows: Iterable[tuple[int, float | None, str | None]], years: int = 3
                       ) -> tuple[float, str, str] | None:
    """(cagr, currency, 'Y0-Y1') from annual local-currency revenue (fiscal_year, value_local, currency_local).

    Uses the latest year with a value and the year `years` earlier; both must be > 0 and in the same currency.
    NULL values are skipped (never read as 0); anything missing -> None."""
    vals = {int(y): (v, c) for y, v, c in rows if v is not None}
    if not vals:
        return None
    y1 = max(vals)
    y0 = y1 - years
    if y0 not in vals:
        return None
    (v1, c1), (v0, c0) = vals[y1], vals[y0]
    if not c1 or c1 != c0 or v1 <= 0 or v0 <= 0:
        return None
    return (v1 / v0) ** (1.0 / years) - 1.0, c1, f"{y0}-{y1}"


def l1_passes(res: dict | None, *, adjacent_min: float = L1_ADJACENT_MIN, core_min: float = L1_CORE_MIN) -> bool:
    """L1 pass: label core (with p_core >= core_min), or label adjacent with p_core + p_adjacent >= adjacent_min."""
    if not res or res.get("status") != "ok":
        return False
    label, probs = res.get("label"), res.get("probs") or {}
    if label == "core":
        return core_min <= 0 or (probs.get("core") or 0.0) >= core_min
    if label == "adjacent":
        return (probs.get("core") or 0.0) + (probs.get("adjacent") or 0.0) >= adjacent_min
    return False


def p_core_of(res: dict | None) -> float | None:
    if not res:
        return None
    p = (res.get("probs") or {}).get("core")
    return float(p) if p is not None else None


def score_of(l2_label: str | None, p_core: float | None, mcap: float | None, evidence: str | None = None) -> float:
    """L2 weight (halved for a positive label read from a profile) + L1_WEIGHT x p_core + a log market-cap tie-break."""
    w = L2_WEIGHTS.get(l2_label or "", 0.0)
    if evidence == "profile" and w > 0 and l2_label in L2_VERIFIED:
        w *= L2_PROFILE_FACTOR
    s = w + L1_WEIGHT * (p_core or 0.0)
    if mcap and mcap > 0:
        s += MCAP_TIEBREAK * math.log10(mcap)
    return s


def score_ev(p_explicit: float | None, p_partial: float | None, p_core: float | None, mcap: float | None,
             evidence: str | None = None) -> float:
    """--rank ev: 3 x mean P(explicit) + 1.5 x mean P(partial) (from a profile: P(explicit) counts as partial and
    the sum is halved, as in score_of)
    + L1_WEIGHT x p_core + the log market-cap tie-break."""
    pe, pp = (p_explicit or 0.0), (p_partial or 0.0)
    if evidence == "profile":                     # a profile supports at most partial (PROFILE_CAP_ZH)
        pe, pp = 0.0, pe + pp
    w = EV_WEIGHTS["explicit"] * pe + EV_WEIGHTS["partial"] * pp
    if evidence == "profile":
        w *= L2_PROFILE_FACTOR
    s = w + L1_WEIGHT * (p_core or 0.0)
    if mcap and mcap > 0:
        s += MCAP_TIEBREAK * math.log10(mcap)
    return s


def p_pos_of(probs: dict[str, float] | None) -> float | None:
    """P(explicit) + P(partial) of one L2 answer (None without probabilities)."""
    if not probs:
        return None
    return float(probs.get("explicit") or 0.0) + float(probs.get("partial") or 0.0)


def aggregate_reads(reads: list[tuple[int, dict]]) -> dict[str, Any] | None:
    """Mean of the ok reads [(read index, result)] of one L2 item, None when no read is ok.

    {'probs': the mean vector (labels in L2_CRITERIA order, then any others), 'label': its argmax (ties -> the
    earlier label in L2_CRITERIA), 'n', 'p_pos': mean p_pos, 'p_pos_sd': population sd of p_pos over the reads (0
    for one read), 'p_explicit', 'p_partial', 'reads': [{read, request_id, p_pos, cached}] of every read given}."""
    ok = [(r, x) for r, x in reads if x.get("status") == "ok" and x.get("probs")]
    detail = [{"read": r, "request_id": x.get("request_id"), "p_pos": p_pos_of(x.get("probs"))
               if x.get("status") == "ok" else None, "cached": bool(x.get("cached"))} for r, x in reads]
    if not ok:
        return None
    labels = list(L2_CRITERIA) + sorted({k for _, x in ok for k in x["probs"]} - set(L2_CRITERIA))
    n = len(ok)
    probs = {k: sum(float(x["probs"].get(k) or 0.0) for _, x in ok) / n for k in labels
             if any(k in x["probs"] for _, x in ok)}
    label = None
    for k in labels:                              # first maximum in L2_CRITERIA order
        if k in probs and (label is None or probs[k] > probs[label]):
            label = k
    pps = [p_pos_of(x["probs"]) or 0.0 for _, x in ok]
    mean = sum(pps) / n
    sd = math.sqrt(sum((p - mean) ** 2 for p in pps) / n)
    return {"probs": probs, "label": label, "n": n, "p_pos": mean, "p_pos_sd": sd,
            "p_explicit": probs.get("explicit", 0.0), "p_partial": probs.get("partial", 0.0), "reads": detail}


def slugify(idea: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", idea.lower())
    slug = "-".join(words)[:40].strip("-")
    return slug or "idea-" + hashlib.sha1(idea.encode("utf-8")).hexdigest()[:8]


def estimate_seconds(requests: int, rate: float = JEV_DEFAULT_RATE, workers: int = JEV_DEFAULT_WORKERS,
                     latency: float = JEV_ASSUMED_LATENCY_S) -> float:
    if not requests:
        return 0.0
    return max(requests / max(rate, 1e-9), math.ceil(requests / max(workers, 1)) * latency)


# ---------------------------------------------------------------------------------------------------------------
# Country filter

ISO2_COUNTRY: dict[str, str] = {
    "US": "United States", "CA": "Canada", "CN": "China", "HK": "Hong Kong", "JP": "Japan", "KR": "South Korea",
    "TW": "Taiwan", "IN": "India", "GB": "United Kingdom", "UK": "United Kingdom", "IE": "Ireland", "DE": "Germany",
    "FR": "France", "NL": "Netherlands", "BE": "Belgium", "LU": "Luxembourg", "CH": "Switzerland", "AT": "Austria",
    "IT": "Italy", "ES": "Spain", "PT": "Portugal", "GR": "Greece", "DK": "Denmark", "SE": "Sweden", "NO": "Norway",
    "FI": "Finland", "PL": "Poland", "AU": "Australia", "NZ": "New Zealand", "SG": "Singapore", "MY": "Malaysia",
    "ID": "Indonesia", "TH": "Thailand", "PH": "Philippines", "VN": "Vietnam", "BR": "Brazil", "MX": "Mexico",
    "AR": "Argentina", "CL": "Chile", "IL": "Israel", "SA": "Saudi Arabia", "AE": "United Arab Emirates",
    "TR": "Turkey", "ZA": "South Africa", "BM": "Bermuda", "KY": "Cayman Islands",
}


def country_matcher(countries: list[str] | None, known_countries: Iterable[str | None] | None = None
                    ) -> Callable[[str | None, str | None], bool]:
    """Tokens may be ISO-2 codes (JP), TradingView country names (Japan) or coverage regions (US, Europe & UK).
    Matching is case-insensitive. A region token matches coverage.region_for(country, exchange).

    A token that is none of these raises ValueError (checked against ISO2_COUNTRY, coverage.REGIONS and, when given,
    the country names in the store), so a typo never silently yields an empty universe.
    Offshore incorporation: a country token matches the country of incorporation only. 'CN' / 'China' therefore
    leaves out Cayman/Bermuda-incorporated Chinese companies; the region 'Hong Kong' (or 'US') catches them by listing
    venue, because coverage.region_for falls back to the exchange for offshore jurisdictions."""
    if not countries:
        return lambda country, exchange: True
    names, regions = set(), set()
    region_lc = {r.lower(): r for r in coverage.REGIONS}
    known = {c.strip().lower() for c in (known_countries or []) if c}
    known |= {v.lower() for v in ISO2_COUNTRY.values()}
    unknown = []
    for tok in countries:
        t = tok.strip()
        if not t:
            continue
        hit = False
        if t.lower() in region_lc:
            regions.add(region_lc[t.lower()])
            hit = True
        if t.upper() in ISO2_COUNTRY:
            names.add(ISO2_COUNTRY[t.upper()].lower())
            hit = True
        if t.lower() in known:
            hit = True
        names.add(t.lower())
        if not hit:
            unknown.append(t)
    if unknown:
        raise ValueError(f"unknown country/region token(s): {', '.join(unknown)} (use ISO-2 codes such as JP, "
                         f"TradingView country names, or regions: {', '.join(coverage.REGIONS)})")

    def match(country: str | None, exchange: str | None) -> bool:
        if country and country.strip().lower() in names:
            return True
        return bool(regions) and coverage.region_for(country, exchange) in regions
    return match


# ---------------------------------------------------------------------------------------------------------------
# Store reads

def load_universe(con, *, min_mcap_usd: float, min_avg_volume: float | None) -> list[dict[str, Any]]:
    sql = """
        SELECT u.security_id, u.company_key, u.name, u.country, u.exchange, u.market_cap_usd, lm.avg_volume_10d
        FROM universe u LEFT JOIN latest_market lm ON lm.security_id = u.security_id
        WHERE u.market_cap_usd IS NOT NULL AND u.market_cap_usd >= ?"""
    params: list[Any] = [min_mcap_usd]
    if min_avg_volume is not None:
        sql += " AND lm.avg_volume_10d IS NOT NULL AND lm.avg_volume_10d >= ?"
        params.append(min_avg_volume)
    sql += " ORDER BY u.market_cap_usd DESC, u.security_id"
    cols = ("security_id", "company_key", "name", "country", "exchange", "market_cap_usd", "avg_volume_10d")
    return [dict(zip(cols, r)) for r in con.execute(sql, params).fetchall()]


def _u_filter(company_keys: list[str] | None) -> str:
    """The WHERE of the universe CTE: the market-cap floor, or (company_keys given) exactly those companies."""
    if company_keys is not None:
        return "company_key IN (SELECT unnest(?::VARCHAR[]))"
    return "market_cap_usd IS NOT NULL AND market_cap_usd >= ?"


def load_universe_all(con) -> list[dict[str, Any]]:
    """Every universe company (any market cap, NULL included) with the fields the run ledger needs, in the ledger's
    order (market cap desc, NULL last, then security_id)."""
    cols = ("company_key", "security_id", "name", "market_cap_usd", "market_as_of")
    return [dict(zip(cols, r)) for r in con.execute(
        "SELECT company_key, security_id, name, market_cap_usd, market_as_of FROM universe "
        "ORDER BY market_cap_usd DESC NULLS LAST, security_id").fetchall()]


def _pre_l1_stages(all_rows: list[dict], min_mcap_usd: float, rows: list[dict], vol_ok: list[dict],
                   universe: list[dict], drops: set[str], no_desc: list[dict]) -> dict[str, str]:
    """company_key -> the pre-L1 stage it stopped at (null_mcap | below_min_mcap | below_min_volume | other_country |
    shell | no_description) or 'described' (it reached L1)."""
    stage: dict[str, str] = {}
    for r in all_rows:
        m = r["market_cap_usd"]
        stage[r["company_key"]] = "null_mcap" if m is None else "below_min_mcap" if m < min_mcap_usd else "described"
    vk, uk, nk = ({r["company_key"] for r in x} for x in (vol_ok, universe, no_desc))
    for r in rows:
        k = r["company_key"]
        stage[k] = ("below_min_volume" if k not in vk else "other_country" if k not in uk else "shell"
                    if k in drops else "no_description" if k in nk else "described")
    return stage


EMPTY_DESC: dict[str, Any] = {"text": "", "plain": "", "sources": [], "tier": None}


def _forced_extras(cfg, sv: dict[str, Any] | None, described: list[dict], stage: dict[str, str],
                   docs: dict[str, dict], notes: list[str]) -> list[dict[str, Any]]:
    """The sieve's forced companies (pins and checks) that a pre-L1 filter removed but that have a description or
    official text: layer 2 reads them anyway (no L1 call) and the report shows the result under 你关心的公司, with
    the filter that removed them ('extra_stage'). Their documents are added to `docs`. One short read session, only
    when such a company exists."""
    if sv is None:
        return []
    from . import calib
    have = {c["company_key"] for c in described} | {c["security_id"] for c in described}
    want = [ex for ex in calib.forced_examples(sv)
            if ex.get("company_key") not in have and ex.get("security_id") not in have]
    if not want:
        return []
    out: list[dict[str, Any]] = []
    with store.session(cfg, read_only=True, wait_s=READ_WAIT_S) as con:
        sids = [ex["security_id"] for ex in want if ex.get("security_id")]
        ck_of = dict(con.execute("SELECT security_id, company_key FROM securities WHERE security_id IN "
                                 "(SELECT unnest(?::VARCHAR[]))", [sids]).fetchall()) if sids else {}
        keys = list(dict.fromkeys(ex.get("company_key") or ck_of.get(ex.get("security_id")) for ex in want))
        keys = [k for k in keys if k]
        cols = ("company_key", "security_id", "name", "country", "exchange", "market_cap_usd", "avg_volume_10d")
        urows = {r[0]: dict(zip(cols, r)) for r in con.execute(
            "SELECT u.company_key, u.security_id, u.name, u.country, u.exchange, u.market_cap_usd, lm.avg_volume_10d "
            "FROM universe u LEFT JOIN latest_market lm ON lm.security_id = u.security_id "
            "WHERE u.company_key IN (SELECT unnest(?::VARCHAR[]))", [keys]).fetchall()} if keys else {}
        xdescs = load_descriptions(con, company_keys=keys) if keys else {}
        xdocs = load_documents(con, company_keys=keys) if keys else {}
    for ex in want:
        k = ex.get("company_key") or ck_of.get(ex.get("security_id"))
        who = ex.get("name") or ex.get("security_id") or ex.get("company_key")
        u = urows.get(k) if k else None
        if u is None:
            notes.append(f"校准：{who} 不在股票池（不是主要上市线或已停用），没有读")
            continue
        if any(c["company_key"] == k for c in out) or stage.get(k) in (None, "described"):
            continue
        d = select_description(xdescs.get(k, []))
        doc = xdocs.get(k)
        if d is None and not (doc and doc.get("text_path") and Path(doc["text_path"]).exists()):
            notes.append(f"校准：{who} 没有简介也没有年报原文，没有读")
            continue
        if doc is not None:
            docs[k] = doc
        out.append({**u, "desc": d or dict(EMPTY_DESC), "extra_stage": stage[k]})
    return out


def load_descriptions(con, *, min_mcap_usd: float = 0.0, company_keys: list[str] | None = None
                      ) -> dict[str, list[tuple[str, str, bool]]]:
    """company_key -> [(source_id, text, on_own_line)] for universe companies with market cap >= min_mcap_usd (or,
    with company_keys, for exactly those companies whatever their market cap: the sieve's forced extras)."""
    sql = f"""
        WITH u AS (SELECT security_id, company_key FROM universe WHERE {_u_filter(company_keys)})
        SELECT u.company_key, d.source_id, d.text, d.security_id = u.security_id AS own
        FROM u JOIN descriptions d ON d.security_id = u.security_id
        WHERE d.text IS NOT NULL AND trim(d.text) <> ''
        UNION
        SELECT u.company_key, d.source_id, d.text, d.security_id = u.security_id AS own
        FROM u JOIN descriptions d ON d.company_key = u.company_key
        WHERE d.text IS NOT NULL AND trim(d.text) <> ''"""
    out: dict[str, list[tuple[str, str, bool]]] = {}
    arg = list(company_keys) if company_keys is not None else min_mcap_usd
    for ck, src, text, own in con.execute(sql, [arg]).fetchall():
        out.setdefault(ck, []).append((src, text, bool(own)))
    return out


_OFFICIAL_SQL = "(" + ", ".join(f"'{s}'" for s in OFFICIAL_DOC_SOURCES) + ")"
_DOC_ANY = f"(doc.source_id LIKE 'sec%' OR doc.source_id IN {_OFFICIAL_SQL}) AND doc.text_path IS NOT NULL"
_DOC_COLS = ("doc_id", "source_id", "form", "section", "url", "text_path", "filing_date", "report_date",
             "text_chars", "extract_note", "extractor", "cik")


def _to_date(v: Any) -> dt.date | None:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, str) and v:
        with contextlib.suppress(ValueError):
            return dt.date.fromisoformat(v[:10])
    return None


def is_summary_doc(doc: dict[str, Any]) -> bool:
    """A short summary (e.g. a CNINFO 年度报告摘要), not the full business section: said by form or section only
    ('summary' / '摘要' / '要約' / '요약'). extract_note and extractor are ignored on purpose: CNINFO full-report rows
    carry notes such as 'summary_failed:...', 'summary_previously_failed' or 'rules=summary'."""
    blob = " ".join(str(doc.get(k) or "") for k in ("form", "section")).lower()
    return any(w in blob for w in ("summary", "摘要", "要約", "요약"))


def fiscal_year_of(doc: dict[str, Any]) -> int | None:
    """Fiscal year of a filing: the year of report_date (period end), else of filing_date. None when neither."""
    d = _to_date(doc.get("report_date")) or _to_date(doc.get("filing_date"))
    return d.year if d else None


def choose_document(cands: list[dict[str, Any]], exists: Callable[[str], bool] | None = None
                    ) -> dict[str, Any] | None:
    """The document layer 2 reads for one company.

    The newest filing wins (fiscal year, then filing date, then doc_id, all descending). A full business section is
    preferred over a short summary only when both are for the same fiscal year and the full text file is present
    (`exists(text_path)`, default Path.exists); a summary of a newer year still beats an older full report."""
    if not cands:
        return None
    exists = exists or (lambda p: Path(p).exists())
    key = lambda d: (fiscal_year_of(d) or 0, _to_date(d.get("filing_date")) or dt.date.min,  # noqa: E731
                     str(d.get("doc_id") or ""))
    ordered = sorted(cands, key=key, reverse=True)
    newest = ordered[0]
    if is_summary_doc(newest):
        fy = fiscal_year_of(newest)
        for d in ordered[1:]:
            if fiscal_year_of(d) == fy and not is_summary_doc(d) and d.get("text_path") and exists(d["text_path"]):
                return d
    return newest


def summary_companion(chosen: dict[str, Any] | None, cands: list[dict[str, Any]],
                      exists: Callable[[str], bool] | None = None) -> dict[str, Any] | None:
    """The same-fiscal-year summary of a chosen FULL report, when one with a text file exists (CNINFO files both).
    Layer 2 reads it first: a 年度报告摘要's 主要业务或产品简介 is a compact business overview, while the full report's
    主要业务 section often opens with the year's results and work log."""
    if not chosen or is_summary_doc(chosen):
        return None
    exists = exists or (lambda p: Path(p).exists())
    fy = fiscal_year_of(chosen)
    pool = [d for d in cands if d is not chosen and is_summary_doc(d) and fiscal_year_of(d) == fy
            and d.get("source_id") == chosen.get("source_id") and d.get("text_path") and exists(d["text_path"])]
    if not pool:
        return None
    return max(pool, key=lambda d: (_to_date(d.get("filing_date")) or dt.date.min, str(d.get("doc_id") or "")))


def load_documents(con, *, min_mcap_usd: float = 0.0, company_keys: list[str] | None = None
                   ) -> dict[str, dict[str, Any]]:
    """company_key -> the official annual-report document layer 2 reads (choose_document), from any source in
    OFFICIAL_DOC_SOURCES with extracted text. Attached by security_id, by company_key, or through the line's native
    identifier (sec_cik / cninfo_orgid / edinet_code / dart_corp_code) -> documents.cik of that source, which covers
    class lines of one issuer with different ISINs (BF.A / BF.B). company_keys: exactly these companies (any market
    cap)."""
    if not coverage._has_table(con, "documents"):
        return {}
    ids = coverage._ids_relation(con)
    have = {r[0] for r in con.execute("SELECT column_name FROM information_schema.columns "
                                      "WHERE table_schema = 'main' AND table_name = 'documents'").fetchall()}
    sel = ", ".join(f"doc.{c}" if c in have else f"NULL AS {c}" for c in _DOC_COLS)
    id_join = " OR ".join(
        f"(i.id_type = '{t}' AND " + ("doc.source_id LIKE 'sec%'" if s == "sec_filing_text" else f"doc.source_id = '{s}'")
        + ")" for t, s in NATIVE_ID_TYPES.items())
    sql = f"""
        WITH u AS (SELECT security_id, company_key FROM universe WHERE {_u_filter(company_keys)})
        SELECT u.company_key, {sel} FROM u JOIN documents doc ON doc.security_id = u.security_id WHERE {_DOC_ANY}
        UNION
        SELECT u.company_key, {sel} FROM u JOIN documents doc ON doc.company_key = u.company_key WHERE {_DOC_ANY}
        UNION
        SELECT u.company_key, {sel} FROM u JOIN {ids} i ON i.security_id = u.security_id
        JOIN documents doc ON doc.cik = i.id_value AND ({id_join}) WHERE {_DOC_ANY}"""
    by_company: dict[str, list[dict[str, Any]]] = {}
    arg = list(company_keys) if company_keys is not None else min_mcap_usd
    for r in con.execute(sql, [arg]).fetchall():
        by_company.setdefault(r[0], []).append({"company_key": r[0], **dict(zip(_DOC_COLS, r[1:]))})
    out = {}
    for ck, cands in by_company.items():
        d = choose_document(cands)
        if d is not None:
            d["candidates"] = len(cands)
            comp = summary_companion(d, cands)
            if comp is not None:
                d["companion"] = comp
            out[ck] = d
    return out


def load_sec_documents(con, *, min_mcap_usd: float) -> dict[str, dict[str, Any]]:
    """Backward-compatible name of load_documents (all official sources, not only SEC)."""
    return load_documents(con, min_mcap_usd=min_mcap_usd)


def load_fundamentals(con, security_ids: list[str]) -> dict[str, dict[str, Any]]:
    """security_id -> {'revenue_ttm_usd', 'cagr', 'cagr_currency', 'cagr_years'} (None where missing)."""
    if not security_ids:
        return {}
    out = {s: {"revenue_ttm_usd": None, "cagr": None, "cagr_currency": None, "cagr_years": None}
           for s in security_ids}
    ph = ",".join("?" for _ in security_ids)
    for sid, v in con.execute(f"""
            SELECT f.security_id, f.value_usd FROM fundamentals_current f
            JOIN (SELECT security_id, max(as_of) AS as_of FROM fundamentals_current
                  WHERE metric = 'total_revenue' AND period = 'ttm' AND security_id IN ({ph}) GROUP BY 1) x
              ON x.security_id = f.security_id AND x.as_of = f.as_of
            WHERE f.metric = 'total_revenue' AND f.period = 'ttm'""", security_ids).fetchall():
        out[sid]["revenue_ttm_usd"] = v
    hist: dict[str, list[tuple[int, float | None, str | None]]] = {}
    for sid, y, v, c in con.execute(f"""
            SELECT security_id, fiscal_year, value_local, currency_local FROM fundamentals_annual
            WHERE metric = 'total_revenue' AND security_id IN ({ph})""", security_ids).fetchall():
        hist.setdefault(sid, []).append((y, v, c))
    for sid, rows in hist.items():
        g = revenue_cagr_local(rows)
        if g:
            out[sid].update(cagr=g[0], cagr_currency=g[1], cagr_years=g[2])
    return out


def universe_counts(con, *, min_mcap_usd: float) -> dict[str, int]:
    """Rows of the universe view before any filter: total, NULL market cap, below the market-cap floor."""
    total, with_mcap, above = con.execute(
        "SELECT count(*), count(market_cap_usd), count(*) FILTER (WHERE market_cap_usd >= ?) FROM universe",
        [min_mcap_usd]).fetchone()
    return {"universe_rows": int(total), "null_mcap": int(total - with_mcap),
            "below_min_mcap": int(with_mcap - above)}


def known_country_names(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT DISTINCT country FROM universe WHERE country IS NOT NULL").fetchall()}


# ---------------------------------------------------------------------------------------------------------------
# Screen

def _not_sent(it, error: str, status: str = "failed") -> dict:
    return {"item_id": it.item_id, "label": None, "probs": {}, "request_id": None, "status": status,
            "error": error, "cached": False}


def _call_classify(client, items, question) -> tuple[list[dict], str | None, str | None]:
    """classify() with Jev errors turned into statuses. Returns (results, run_status_or_None, error_text).

    On JevUnavailable the results the client had already completed (e.results: answers already paid for) are kept;
    items without a result come back 'failed' ('not sent: provider unavailable')."""
    try:
        res = list(client.classify(items, question))
    except Exception as e:  # noqa: BLE001 - only the contract errors are handled, anything else propagates
        if _is_error(e, "JevUnavailable"):
            status = STATUS_BUSY if _is_error(e, "JevBusy") else STATUS_UNAVAILABLE
            partial = getattr(e, "results", None) or []
            by_id = {r.get("item_id"): r for r in partial if r}
            out = [by_id.get(it.item_id) or _not_sent(it, "not sent: provider unavailable") for it in items]
            return out, status, f"{type(e).__name__}: {e}"
        if _is_error(e, "BudgetExceeded"):
            return ([_not_sent(it, str(e)[:300], "skipped_budget") for it in items], STATUS_BUDGET,
                    f"{type(e).__name__}: {e}")
        raise
    by_id = {r.get("item_id"): r for r in res}
    ordered = [by_id.get(it.item_id) or _not_sent(it, "no result returned") for it in items]
    return ordered, None, None


def band_read_indices(reads: int, read_offset: int = 0) -> list[int]:
    """The extra read indices a band item gets: 1..K-1, or with read_offset N the fresh reads N..N+K-1 that replace
    its read 0 (the live churn check)."""
    if read_offset > 0:
        return list(range(read_offset, read_offset + reads))
    return list(range(1, reads))


def _l2_band_reads(client, items: list, res: dict[str, dict], q2, reads: int, read_offset: int = 0
                   ) -> dict[str, Any]:
    """Extra L2 reads of the band items, after the base (read 0) L2 read.

    Band = items whose read-0 result is ok with p_pos (P(explicit) + P(partial)) in L2_BAND. For each read index r
    (band_read_indices) the band items alone are sent under dataclasses.replace(q2, read=r), ordered by
    sha256(f"{r}:{item_id}"), so packet neighbours and positions change between reads while every read stays
    deterministic and cached. Stops at the first budget / provider stop (the remaining reads are skipped).
    Returns {'band': [item_id], 'reads': {item_id: [(read, result)]}, 'planned', 'ok', 'skipped', 'cost_usd',
    'status', 'error'}; 'reads' holds only the extra reads."""
    band = [it for it in items if (res.get(it.item_id) or {}).get("status") == "ok"
            and L2_BAND[0] <= (p_pos_of(res[it.item_id].get("probs")) or 0.0) <= L2_BAND[1]]
    idx = band_read_indices(reads, read_offset)
    out: dict[str, Any] = {"band": [it.item_id for it in band], "reads": {it.item_id: [] for it in band},
                           "planned": len(band) * len(idx), "ok": 0, "skipped": 0, "cost_usd": 0.0,
                           "status": None, "error": None}
    if not band or not idx or client is None:
        out["skipped"] = out["planned"] if client is None else 0
        return out
    spent0, _ = _client_stats(client)
    for n, r in enumerate(idx):
        q = dataclasses.replace(q2, read=r)
        order = sorted(band, key=lambda it: hashlib.sha256(f"{r}:{it.item_id}".encode("utf-8")).hexdigest())
        got, err_status, err = _call_classify(client, order, q)
        for it, x in zip(order, got):
            out["reads"][it.item_id].append((r, x))
            out["ok" if x.get("status") == "ok" else "skipped"] += 1
        if err_status:
            out.update(status=err_status, error=err)
            out["skipped"] += len(band) * (len(idx) - n - 1)
            break
    out["cost_usd"] = round(_client_stats(client)[0] - spent0, 6)
    return out


def _client_stats(client) -> tuple[float, int]:
    if client is None:
        return 0.0, 0
    return float(getattr(client, "spent_usd", 0.0) or 0.0), int(getattr(client, "requests_sent", 0) or 0)


def _estimate(client, items, question) -> dict[str, Any] | None:
    if client is None or not items:
        return {"requests": 0, "items": 0, "est_input_tokens": 0, "est_cost_usd": 0.0, "est_reserved_usd": 0.0,
                "basis": "no items", "est_seconds": 0.0}
    try:
        est = dict(client.estimate(items, question))
    except Exception as e:  # noqa: BLE001 - an estimate must never stop the screen
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    est["est_seconds"] = round(estimate_seconds(
        int(est.get("requests") or 0), float(getattr(client, "rate_per_second", JEV_DEFAULT_RATE) or JEV_DEFAULT_RATE),
        int(getattr(client, "workers", JEV_DEFAULT_WORKERS) or JEV_DEFAULT_WORKERS)), 1)
    return est


def _age_days(filing_date: Any, today: dt.date) -> int | None:
    if isinstance(filing_date, dt.datetime):
        filing_date = filing_date.date()
    if isinstance(filing_date, dt.date):
        return (today - filing_date).days
    return None


def source_label(source_id: str | None) -> str:
    if not source_id:
        return "filing"
    if source_id in OFFICIAL_DOC_SOURCES:
        return OFFICIAL_DOC_SOURCES[source_id][0]
    return "SEC" if source_id.startswith("sec") else source_id


def terms_for(terms_by_lang: dict[str, list[str]] | list[str] | None, lang: str | None) -> list[str]:
    """Excerpt terms for a document language. A plain list means English terms (the old contract)."""
    if terms_by_lang is None:
        return []
    if isinstance(terms_by_lang, (list, tuple)):
        return list(terms_by_lang) if lang in (None, "en") else []
    return list(terms_by_lang.get(lang or "en") or [])


def _evidence_excerpt(kw: dict[str, str] | None, first: dict[str, str], terms: list[str] | None,
                      lang: str | None) -> str:
    """evidence_excerpt (the output table / report column, OUTPUT_EXCERPT_CHARS): the keyword excerpt, else the
    first excerpt, cut from its start. When that cut loses every term of the keyword excerpt (a CJK sentence-end
    cut, or a hit past OUTPUT_EXCERPT_CHARS), the window moves onto the hits instead (_keyword_window)."""
    tlang = lang if lang in CJK_LANGS else None
    text = truncate((kw or first)["text"], OUTPUT_EXCERPT_CHARS, tlang)
    pats = [_term_pattern(t) for t in terms or []] if kw else []
    if pats and not any(p.search(text) for p in pats):
        text = _keyword_window(kw["text"], pats, OUTPUT_EXCERPT_CHARS, tlang)
    return text


_TAG_LINE = re.compile(r"\[[^\n]*\]")


def evidence_sha(text: str | None) -> str | None:
    """sha256 (first 16 hex chars) of an L2 input text without its first '[...]' tag line: a tag-only change (a new
    filing date in the tag, ' + summary') keeps the sha, so an answered calibration card is not asked again."""
    if text is None:
        return None
    first, sep, rest = text.partition("\n")
    body = rest.lstrip("\n") if _TAG_LINE.fullmatch(first.strip()) else text
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def matched_terms(text: str | None, terms: Iterable[str]) -> list[str]:
    """The terms (in their given order, deduplicated) that match `text` (_term_pattern)."""
    if not text:
        return []
    out = []
    for t in terms:
        if t and t not in out and _term_pattern(t).search(text):
            out.append(t)
    return out


def _l2_input(c: dict[str, Any], doc: dict[str, Any] | None, terms: dict[str, list[str]] | list[str] | None,
              today: dt.date | None = None, weak: dict[str, list[str]] | list[str] | None = None) -> dict[str, Any]:
    """Evidence text for one company: annual-report excerpts when a readable official document exists (any source
    in OFFICIAL_DOC_SOURCES), else its description(s).

    The text starts with a tag saying which it is ('[annual report excerpts: SEC 10-K filed YYYY-MM-DD; language
    en]' or '[company profile; no annual report text available]'), so the model and the reader know what was
    judged. Excerpt keywords are the ones for the document's language (detect_language, the source's own language
    when the text is too short to tell); `weak` (same shape) are the sieve's down-weighted terms (build_excerpts).
    Records evidence_sha (tag line ignored) and matched_terms (the terms, weak ones included, in the keyword
    excerpt) for l2_inputs.jsonl and the calibration cards."""
    today = today or store.now_utc().date()
    note = None
    if doc and doc.get("text_path"):
        src = doc.get("source_id") or ""
        label = source_label(src)
        try:
            text = Path(doc["text_path"]).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            text, note = None, f"{label} text unreadable: {type(e).__name__}"
        comp, ctext = (doc.get("companion") if text else None), ""
        if comp and comp.get("text_path"):
            with contextlib.suppress(OSError):
                ctext = Path(comp["text_path"]).read_text(encoding="utf-8", errors="replace").strip()
        if ctext:
            text = ctext + "\n\n" + text   # the summary's business overview first (summary_companion)
        else:
            comp = None
        if text:
            lang = detect_language(text, default=OFFICIAL_DOC_SOURCES.get(src, ("", "en"))[1])
            lterms, lweak = terms_for(terms, lang), terms_for(weak, lang)
            ex = build_excerpts(text, terms=lterms, description=c["desc"]["plain"], lang=lang, weak_terms=lweak)
            if ex:
                kw = next((e for e in ex if e["kind"] == "keywords"), None)
                form, fdate = doc.get("form"), doc.get("filing_date")
                age = _age_days(fdate, today)
                tag = (f"[annual report excerpts: {label} {form or 'filing'}{' + summary' if comp else ''} filed "
                       f"{fdate or 'date unknown'}; language {lang}]")
                body = tag + "\n\n" + "\n\n[...]\n\n".join(e["text"] for e in ex)
                return {"text": body, "excerpts": ex,
                        "evidence": "annual_report", "keyword_hit": kw is not None,
                        "evidence_excerpt": _evidence_excerpt(kw, ex[0], lterms + lweak, lang),
                        "evidence_sha": evidence_sha(body),
                        "matched_terms": matched_terms((kw or {}).get("text"), lterms + lweak),
                        "evidence_url": doc.get("url"), "input_tier": _tier(src)
                        or provenance.LicenseTier.OFFICIAL_PRIVATE.value,
                        "input_source": f"{src or 'document'}:{form or '?'}", "source_id": src or None,
                        "form": form, "lang": lang, "doc_id": doc.get("doc_id"),
                        "filing_date": str(fdate) if fdate else None, "summary": is_summary_doc(doc),
                        "companion_doc_id": (comp or {}).get("doc_id"),
                        "stale": age is not None and age > SEC_MAX_AGE_DAYS, "age_days": age, "note": None}
        note = note or f"{label} text empty"
    body = "[company profile; no annual report text available]\n\n" + c["desc"]["text"]
    return {"text": body, "excerpts": None, "evidence_sha": evidence_sha(body), "matched_terms": [],
            "evidence": "profile", "keyword_hit": None,
            "evidence_excerpt": truncate(c["desc"]["plain"], OUTPUT_EXCERPT_CHARS), "evidence_url": None,
            "input_tier": c["desc"]["tier"] or provenance.LicenseTier.GRAY_PRIVATE.value, "input_source": "profile",
            "source_id": None, "form": None, "lang": None, "doc_id": None, "filing_date": None, "summary": False,
            "stale": False, "age_days": None, "note": note}


# ---------------------------------------------------------------------------------------------------------------
# Keywords / automatic translation (local model, free)

def _default_keywords(cfg, idea: str) -> dict[str, Any]:
    from . import keywords
    return keywords.generate(cfg, idea)


def _clean_terms(v: Any) -> list[str]:
    if not v:
        return []
    if isinstance(v, str):
        v = re.split(r"[,，、;；]", v)
    seen, out = set(), []
    for t in v:
        t = str(t).strip() if t is not None else ""
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def resolve_keywords(cfg, idea: str, *, keywords: list[str] | None, keywords_by_lang: dict[str, list[str]] | None,
                     translate: bool, keywords_fn: Callable | None,
                     sieve_kw: dict[str, dict[str, list[str]]] | None = None,
                     author: dict[str, Any] | None = None, idea_en: str | None = None) -> dict[str, Any]:
    """Decide the idea_en and the excerpt terms per document language.

    Automatic translation runs when translate is on and the idea is not English (needs_translation: Han / kana /
    Hangul, or mostly non-Latin letters; smart quotes and accents do not count). English --keywords do not turn it
    off: the idea is still translated for the Jev questions, and the user's keywords stay the English excerpt terms.
    Precedence per language: the user's flags (--keywords for en, --keywords-zh/-ja/-ko), then the generated keywords,
    then (en) the Latin words of idea_en or the idea, (zh/ja/ko) the idea's own runs in that script.
    Returns {'idea_en', 'terms': {lang: [...]}, 'status': 'generated' | 'user' | 'not_needed' | 'disabled' |
    'unavailable', 'user_keywords': bool, 'model', 'cached', 'seconds', 'error', 'generated': {lang: [...]} | None}.
    'user' means English --keywords with an English idea (nothing to translate).
    sieve_kw ({lang: {'add': [...], 'weak': [...], 'log': [...]}}, the sieve's keywords): 'add' terms are appended
    to whatever the precedence above chose (never replacing the user's flags), and 'weak' terms are taken out of
    the terms and returned separately in info['weak'] ({lang: [...]}; build_excerpts down-weights them, it does not
    delete them). Without sieve_kw the terms are unchanged and info['weak'] is {}.
    author (sieve_author.author_keywords: the sieve's idea_en / seed_terms written by the user's AI): an idea_en there
    replaces the local model (status 'sieve', the model is not called); a frozen one (author['frozen']: the idea_en
    of the last paid run, possibly None) likewise (status 'frozen'); seed_terms[lang] come after the user's flags
    and before the generated terms.
    idea_en (the English sentence the caller supplied: `quickstart` / `screen --idea-en`): used as is and
    before the author's, status 'agent', the local model is not called and no 'translation unavailable' warning follows."""
    user = {lang: _clean_terms((keywords_by_lang or {}).get(lang)) for lang in LANGS}
    user["en"] = _clean_terms(keywords) or user["en"]
    info: dict[str, Any] = {"idea_en": None, "status": "not_needed", "model": None, "cached": None, "seconds": None,
                            "error": None, "generated": None, "user_keywords": bool(user["en"])}
    gen: dict[str, list[str]] = {}
    given_en = str(idea_en or "").strip() or None
    if given_en is not None:
        info.update(status="agent", idea_en=given_en)
    elif not needs_translation(idea):
        info["status"] = "user" if user["en"] else "not_needed"
    elif not translate:
        info["status"] = "disabled"
    elif (author or {}).get("frozen"):
        info.update(status="frozen", idea_en=author.get("idea_en"))
    elif (author or {}).get("idea_en"):
        info.update(status="sieve", idea_en=author["idea_en"])
    else:
        t = time.monotonic()
        try:
            out = (keywords_fn or _default_keywords)(cfg, idea)
        except Exception as e:  # noqa: BLE001 - KeywordsUnavailable or any local-model failure: old behaviour
            info.update(status="unavailable", error=f"{type(e).__name__}: {str(e)[:300]}")
        else:
            out = out if isinstance(out, dict) else {}
            kw = out.get("keywords") if isinstance(out.get("keywords"), dict) else {}
            gen = {lang: _clean_terms(kw.get(lang)) for lang in LANGS}
            idea_en = str(out.get("idea_en") or "").strip() or None
            info.update(status="generated", idea_en=idea_en, model=out.get("model"), cached=out.get("cached"),
                        generated=gen)
        info["seconds"] = round(time.monotonic() - t, 2)
    seeds = {lang: _clean_terms(v) for lang, v in ((author or {}).get("seed_terms") or {}).items()}
    terms: dict[str, list[str]] = {}
    terms["en"] = user["en"] or seeds.get("en") or gen.get("en") or idea_terms(info["idea_en"] or idea, None)
    for lang in ("zh", "ja", "ko"):
        terms[lang] = user[lang] or seeds.get(lang) or gen.get(lang) or cjk_idea_terms(idea, lang)
    info["seed_terms"] = bool(seeds)
    weak: dict[str, list[str]] = {}
    for lang, kw in (sieve_kw or {}).items():
        if lang not in terms or not isinstance(kw, dict):
            continue
        w = _clean_terms(kw.get("weak"))
        wl = {unicodedata.normalize("NFKC", t).lower() for t in w}
        terms[lang] = [t for t in _clean_terms(terms[lang] + _clean_terms(kw.get("add")))
                       if unicodedata.normalize("NFKC", t).lower() not in wl]
        if w:
            weak[lang] = w
    info["terms"] = terms
    info["weak"] = weak
    return info


def _default_out_dir(cfg, idea: str, started: dt.datetime) -> Path:
    base = Path(cfg.home) / "screens"
    name = f"{started.strftime('%Y%m%d-%H%M')}-{slugify(idea)}"
    path, n = base / name, 2
    while path.exists():
        path, n = base / f"{name}-{n}", n + 1
    return path


def _result_row(run_id: str, c: dict, layer: str, r: dict, inp: dict | None, agg: dict | None = None) -> tuple:
    probs = r.get("probs") or {}
    p_top = max(probs.values()) if probs else None
    if layer == "l1":
        src, tier, url, excerpt = "+".join(c["desc"]["sources"]), c["desc"]["tier"], None, None
    else:
        src, tier, url, excerpt = inp["input_source"], inp["input_tier"], inp["evidence_url"], inp["evidence_excerpt"]
    row = (run_id, c["company_key"], c["security_id"], layer, r.get("label"),
           json.dumps(probs, sort_keys=True) if probs else None, p_top, r.get("request_id"), src, tier, url,
           excerpt, r.get("status"), (str(r["error"])[:500] if r.get("error") else None))
    if layer == "l1":
        return row
    return row + ((json.dumps(agg["reads"], sort_keys=True), agg["p_pos"], agg["p_pos_sd"]) if agg
                  else (None, None, None))


RESULT_COLS = ("run_id", "company_key", "security_id", "layer", "label", "probs_json", "p_top", "request_id",
               "input_source", "input_tier", "evidence_url", "evidence_excerpt", "status", "error")
RESULT_COLS_L2 = RESULT_COLS + ("reads_json", "p_pos", "p_pos_sd")    # L2 rows: the repeated reads (aggregate_reads)
RUN_COLS = ("run_id", "idea", "params_json", "started_at", "finished_at", "status", "universe_n", "l1_n",
            "l1_pass_n", "l2_n", "output_n", "cost_usd", "output_dir", "note")


def _db_write(cfg, fn: Callable[[Any], None], notes: list[str], what: str) -> bool:
    try:
        with store.session(cfg, wait_s=WRITE_WAIT_S) as con:
            fn(con)
        return True
    except store.StoreLocked as e:
        notes.append(f"{what}: store locked ({str(e)[:120]})")
        return False


def _check_params(min_mcap_usd, min_avg_volume, max_out, budget_usd, l2_max, reads=1, read_offset=0,
                  rank="label") -> None:
    def finite_nonneg(name, v):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            raise ValueError(f"{name} must be a finite number >= 0 (got {v!r})")
    finite_nonneg("min_mcap_usd", min_mcap_usd)
    finite_nonneg("budget_usd", budget_usd)
    if min_avg_volume is not None:
        finite_nonneg("min_avg_volume", min_avg_volume)
    if isinstance(max_out, bool) or not isinstance(max_out, int) or max_out < 1:
        raise ValueError(f"max_out must be an integer >= 1 (got {max_out!r})")
    if isinstance(l2_max, bool) or not isinstance(l2_max, int) or l2_max < 0:
        raise ValueError(f"l2_max must be an integer >= 0 (got {l2_max!r})")
    if isinstance(reads, bool) or not isinstance(reads, int) or reads < 1:
        raise ValueError(f"reads must be an integer >= 1 (got {reads!r})")
    if isinstance(read_offset, bool) or not isinstance(read_offset, int) or read_offset < 0:
        raise ValueError(f"read_offset must be an integer >= 0 (got {read_offset!r})")
    if rank not in RANKS:
        raise ValueError(f"rank must be one of {', '.join(RANKS)} (got {rank!r})")


def _counts(res: dict[str, dict], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in res.values():
        v = r.get(key) or "?"
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))


# ---------------------------------------------------------------------------------------------------------------
# Base run (--from-run), sieve (calibration) and the ranking step

class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


UNSET: Any = _Unset()    # "not given": with from_run the base run's params fill it, else SCREEN_DEFAULTS

# screen() parameters a from_run inherits from its base run's params_json (explicit arguments override them).
# dry_run, retry_uncertain, read_offset, out_dir and the sieve are per run and never inherited.
SCREEN_DEFAULTS: dict[str, Any] = {
    "min_mcap_usd": 2e8, "min_avg_volume": None, "countries": None, "max_out": 40, "budget_usd": 3.0, "l2_max": 600,
    "keywords": None, "l1_adjacent_min": L1_ADJACENT_MIN, "l1_core_min": L1_CORE_MIN, "keywords_zh": None,
    "keywords_ja": None, "keywords_ko": None, "translate": True, "reads": L2_READS_DEFAULT, "rank": "label",
    "shells": "drop"}


def resolve_from_run(from_run: str | os.PathLike | None) -> str | None:
    """The run id of a from_run target: a run id as it is, or a run's output directory / its results.json (the
    run_id recorded in it). ValueError (Chinese + English) for a directory without results.json, unreadable JSON or a
    dry run (no stored answers)."""
    if from_run is None or str(from_run).strip() == "":
        return None
    text = str(from_run).strip()
    p = Path(text).expanduser()
    if not (p.exists() or text.endswith(".json") or os.sep in text):
        return text                                     # a run id (scr-...)
    if p.is_dir():
        p = p / "results.json"
    try:
        res = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"from_run: {p} is not a readable results.json ({type(e).__name__}); give a run id "
                         "(scr-...), a run's output directory or its results.json / 读不了这个结果文件") from None
    if not isinstance(res, dict) or not res.get("run_id"):
        raise ValueError(f"from_run: {p} is not a jevscreen results.json (no run_id)")
    if res.get("dry_run"):
        raise ValueError(f"from_run: {p} is a dry run (no stored answers) / 这是 dry run，没有可沿用的结果")
    return str(res["run_id"])


def load_base_run(con, run_id: str) -> dict[str, Any]:
    """{'run_id', 'idea', 'params', 'status', 'l1': {company_key: L1 result}} of an earlier screen run: its
    params_json and its stored L1 answers (screen_results layer l1, returned in the shape classify returns them,
    cached=True). Raises ValueError for an unknown run or one without stored L1 answers (a dry run)."""
    row = con.execute("SELECT idea, params_json, status FROM screen_runs WHERE run_id = ?", [run_id]).fetchone()
    if row is None:
        raise ValueError(f"from_run: unknown screen run {run_id!r}")
    try:
        params = json.loads(row[1] or "{}")
    except ValueError:
        params = {}
    l1: dict[str, dict] = {}
    for ck, label, probs_json, rid, status, error in con.execute(
            "SELECT company_key, label, probs_json, request_id, status, error FROM screen_results "
            "WHERE run_id = ? AND layer = 'l1' ORDER BY company_key", [run_id]).fetchall():
        l1[ck] = {"item_id": ck, "label": label, "probs": json.loads(probs_json) if probs_json else {},
                  "request_id": rid, "status": status or "failed", "error": error, "cached": True}
    if not l1:
        raise ValueError(f"from_run: run {run_id!r} has no stored L1 answers (a dry run, or it stopped before L1)")
    l2_labels = dict(con.execute("SELECT company_key, label FROM screen_results WHERE run_id = ? AND layer = 'l2' "
                                 "AND status = 'ok'", [run_id]).fetchall())
    return {"run_id": run_id, "idea": row[0], "params": params if isinstance(params, dict) else {},
            "status": row[2], "l1": l1, "l2_p0": _base_l2_first_reads(con, run_id), "l2_labels": l2_labels}


def _base_l2_first_reads(con, run_id: str) -> dict[str, float]:
    """{company_key: p_pos of the first read} of a run's ok L2 answers (read 0 when stored, else the first read;
    a run made before the repeated-read columns counts its only answer): what sizes the band of a dry run from it."""
    have = {r[0] for r in con.execute("SELECT column_name FROM information_schema.columns "
                                      "WHERE table_name = 'screen_results'").fetchall()}
    rj = "reads_json" if "reads_json" in have else "NULL AS reads_json"
    out: dict[str, float] = {}
    for ck, probs_json, reads_json in con.execute(
            f"SELECT company_key, probs_json, {rj} FROM screen_results WHERE run_id = ? AND layer = 'l2' "
            "AND status = 'ok'", [run_id]).fetchall():
        reads = []
        with contextlib.suppress(TypeError, ValueError):
            reads = [d for d in json.loads(reads_json or "[]") if isinstance(d, dict) and d.get("p_pos") is not None]
        if reads:
            first = min(reads, key=lambda d: d.get("read") if isinstance(d.get("read"), int) else 10 ** 6)
            out[ck] = float(first["p_pos"])
        else:
            with contextlib.suppress(TypeError, ValueError):
                p = p_pos_of(json.loads(probs_json) if probs_json else None)
                if p is not None:
                    out[ck] = p
    return out


def resolve_sieve(cfg, idea: str, sieve: Any) -> tuple[dict[str, Any] | None, Path | None]:
    """(sieve, path) for screen(sieve=...): None / 'none' -> no sieve; 'auto' -> data/sieves/<idea_key>.json when
    it exists; a path -> that file (ValueError when missing or invalid); a dict -> used as is (tests, rerank)."""
    if sieve is None or sieve == "none":
        return None, None
    from . import calib
    if isinstance(sieve, dict):
        return sieve, None
    if sieve == "auto":
        path = calib.sieve_path(cfg, idea)
        sv = calib.load_sieve(path)
        return sv, (path if sv is not None else None)
    path = Path(sieve)
    sv = calib.load_sieve(path)
    if sv is None:
        raise ValueError(f"sieve not found: {path}")
    return sv, path


def _order(e: dict[str, Any]) -> tuple:
    return -e["score"], -(e["market_cap_usd"] or 0), e["security_id"]


def pin_and_rank(verified: list[dict[str, Any]], unverified: list[dict[str, Any]], sieve: dict[str, Any] | None,
                 max_out: int) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    """The ranking step with the sieve's pins (screen step 4; calib.rerank_result uses the same code, so its free diff
    equals what a rerun produces when no rule changes). Entries are dicts with company_key, security_id, name,
    market_cap_usd, l1_p_core, l2_label, l2_evidence and score.

    Returns (verified', unverified', excluded_by_user, notes): calib.apply_pins, then both lists sorted (score desc,
    market cap desc, security_id). A verified' row in the top max_out that was not there before the pins and is not
    itself a user yes is flagged backfill=True (递补，未经确认: it moved up only because others were removed)."""
    from . import calib
    before = {e["company_key"] for e in sorted(verified, key=_order)[:max_out]}
    v2, excluded, notes = calib.apply_pins(verified, unverified, sieve)
    moved = {e["company_key"] for e in v2} | {e["company_key"] for e in excluded}
    unv2 = [e for e in unverified if e["company_key"] not in moved]
    v2.sort(key=_order)
    unv2.sort(key=_order)
    for i, e in enumerate(v2):
        e["backfill"] = i < max_out and e["company_key"] not in before and e.get("user_verdict") is None
    return v2, unv2, excluded, notes


def output_entries(verified: list[dict[str, Any]], max_out: int) -> list[tuple[int, dict[str, Any]]]:
    """[(rank, entry)] of the output rows of a sorted verified' list: the top max_out, plus every user yes pin ranked
    below them (a yes is always listed: it keeps its real rank and is flagged below_cut, never silently dropped)."""
    out = []
    for i, e in enumerate(verified, 1):
        if i <= max_out:
            out.append((i, e))
        elif e.get("user_verdict") in ("explicit", "partial"):
            e["below_cut"] = True
            out.append((i, e))
    return out


def _pin_of(pins: dict[str, dict], c: dict[str, Any]) -> dict[str, Any] | None:
    return pins.get(c["company_key"]) or pins.get(c["security_id"])


def screen(cfg, idea: str, *, min_mcap_usd: float = UNSET, min_avg_volume: float | None = UNSET,
           countries: list[str] | None = UNSET, max_out: int = UNSET, budget_usd: float = UNSET,
           l2_max: int = UNSET, keywords: list[str] | None = UNSET, dry_run: bool = False, jev_factory=None,
           out_dir=None, l1_adjacent_min: float = UNSET, l1_core_min: float = UNSET, retry_uncertain: bool = False,
           keywords_zh: list[str] | None = UNSET, keywords_ja: list[str] | None = UNSET,
           keywords_ko: list[str] | None = UNSET, translate: bool = UNSET, keywords_fn: Callable | None = None,
           reads: int = UNSET, read_offset: int = 0, from_run: str | None = None, sieve: Any = None,
           rank: str = UNSET, shells: str = UNSET, supersedes: str | None = None,
           fetch_info: dict[str, Any] | None = None, idea_en: str | None = UNSET,
           progress: Callable | None = None, l1_new: bool = False) -> dict[str, Any]:
    """Run one screen. Returns the result dict that is also written to results.json.

    result['status']: ok | partial (some L2 skipped/failed, band reads skipped, fundamentals unreadable) |
    budget_exhausted (budget ran out before L1 finished) | jev_unavailable | jev_busy (another process is using Jev) |
    dry_run. Outputs are written in every case (partial report on failures). result['rows'] holds only L2-verified
    companies (explicit/partial) and user yes pins; result['unverified'] the other L1 passes (not contradicted) with
    their l2_status; result['excluded_by_user'] the companies a user 'no' pin removed.

    retry_uncertain=True resends items whose earlier send had an unknown outcome (it may have been charged).
    keywords (English) and keywords_zh / _ja / _ko pick the excerpt paragraphs of documents in that language.
    translate=True (default): a non-English idea (needs_translation; also with English --keywords) is translated by
    the local model
    (keywords_fn, default jevscreen.keywords.generate; free, also in a dry run) before L1; if it is unavailable the
    idea is used verbatim with a loud warning. result['keywords'] records idea_en, the terms, model and timing.

    Stability: reads=K (default L2_READS_DEFAULT) gives the L2 items whose read-0 p_pos lies in L2_BAND K-1 more
    cached reads (Question.read) and decides them on the mean (aggregate_reads); reads=1 is the single-read screen.
    read_offset=N replaces the band's read 0 with the fresh reads N..N+K-1 (live churn check).
    from_run=RUN_ID (or the run's output directory / results.json): that run's params are the defaults (explicit arguments override) and its stored L1 answers are
    used instead of calling Jev (L1 costs $0; described companies absent from it do not pass L1, counted in notes).
    l1_new=True (requires from_run; the incremental re-rank after a description fill): the described companies that
    have no L1 answer in the base run (newly described) are read by L1 now, everyone else keeps the base run's L1
    answer ($0, never re-read or re-priced); L2 then reads the passes as usual (unchanged inputs are Jev cache hits).
    sieve: None / 'none' | 'auto' | a path | a dict (resolve_sieve): its rules extend the L2 criteria
    (build_l2_question), its keywords extend / down-weight the excerpt terms, its pins apply in the ranking step
    (pin_and_rank) and its pinned / checked companies are always read by L2 (l2_forced), even when L1 missed them.
    rank: 'label' (score_of, default) | 'ev' (score_ev on the mean probabilities; measured, not the default).
    shells: 'drop' (default) | 'keep': the shells filter before L1 (jevscreen.shells: blank-check companies are
    removed, never a company the sieve names; ST / *ST and spac_like are only marks). A --from-run base made before
    the filter existed is inherited as 'keep'. Every run with an output directory also writes funnel.jsonl.gz (one
    line per universe company: the stage it stopped at, its L1 / L2 answer; what `jevscreen why` reads).
    supersedes=RUN_ID (requires from_run=RUN_ID, the on-demand update pass): the outputs replace that run's files in
    out_dir atomically (the old results.json is kept as results.v<N>.json), and only when this run's status is ok or
    partial and not worse than the base run's (ok < partial); otherwise the folder is left untouched and the run's
    screen_runs row gets no output_dir. fetch_info (jevscreen.ondemand.fetch_info): layers['fetch'], per L2 input
    'doc_fetch' (stored | fetched_now | a reason code), gaps['l2_doc_unavailable'] and the l2_doc_* funnel keys;
    without it none of these keys appear. The library never fetches anything itself.
    idea_en: an English sentence for the Jev questions supplied by the caller (quickstart, screen --idea-en);
    resolve_keywords status 'agent', no local model, before the sieve's author idea_en. UNSET: (from_run) the base
    run's caller-supplied idea_en, else the sieve's author idea_en (sieve_author.author_keywords: frozen after a paid
    run), else the automatic translation. With from_run, a base run that has an idea_en
    refuses a different one (ValueError: its L1 answers were given for that English question).
    progress(phase, done, total): phase boundaries and per-request heartbeats ('l1', 'l2'). On-demand annual
    reports are fetched after the run (jevscreen.ondemand_cli.run_fetch: screen --fetch-docs, fetch-docs,
    quickstart), never inside it.
    Raises ValueError for bad parameters, an unknown country token, an unknown / mismatched base run or an invalid
    sieve (including a rule text that would carry a company name or ticker: calib.rule_text_problems), before
    anything is sent or written."""
    if not idea or not idea.strip():
        raise ValueError("idea must not be empty")
    given = {"min_mcap_usd": min_mcap_usd, "min_avg_volume": min_avg_volume, "countries": countries,
             "max_out": max_out, "budget_usd": budget_usd, "l2_max": l2_max, "keywords": keywords,
             "l1_adjacent_min": l1_adjacent_min, "l1_core_min": l1_core_min, "keywords_zh": keywords_zh,
             "keywords_ja": keywords_ja, "keywords_ko": keywords_ko, "translate": translate, "reads": reads,
             "rank": rank, "shells": UNSET if shells is None else shells}
    base = None
    from_run = resolve_from_run(from_run)
    if supersedes is not None and supersedes != from_run:
        raise ValueError("supersedes must name the from_run base run (the on-demand update pass)")
    if l1_new and not from_run:
        raise ValueError("l1_new needs from_run (it reads only the companies the base run has no L1 answer for)")
    if from_run:
        with store.session(cfg, read_only=True, wait_s=READ_WAIT_S) as con:
            base = load_base_run(con, from_run)
        if (base["idea"] or "").strip() != idea.strip():
            raise ValueError(f"from_run: run {from_run!r} screened another idea ({(base['idea'] or '')[:60]!r}); its "
                             "L1 answers do not apply")
    inherited = {k: v for k, v in (base["params"] if base else {}).items() if k in SCREEN_DEFAULTS}
    if base is not None and "shells" not in base["params"]:
        inherited["shells"] = "keep"      # a run made before the shells filter read every company
    eff = {k: (v if v is not UNSET else inherited[k] if k in inherited else SCREEN_DEFAULTS[k])
           for k, v in given.items()}
    base_en = str(((base or {}).get("params") or {}).get("idea_en") or "").strip() or None
    if idea_en is UNSET:
        idea_en = None
        if base is not None and (base.get("params") or {}).get("keywords_status") == "agent" and base_en:
            idea_en = base_en
    elif idea_en is not None and base_en and str(idea_en).strip() != base_en:
        raise ValueError(f"from_run: run {from_run!r} asked Jev in English as {base_en[:80]!r}; a different idea_en "
                         "would not match its L1 answers (start a new screen without --from-run)")
    min_mcap_usd, min_avg_volume, countries = eff["min_mcap_usd"], eff["min_avg_volume"], eff["countries"]
    max_out, budget_usd, l2_max, keywords = eff["max_out"], eff["budget_usd"], eff["l2_max"], eff["keywords"]
    l1_adjacent_min, l1_core_min = eff["l1_adjacent_min"], eff["l1_core_min"]
    keywords_zh, keywords_ja, keywords_ko = eff["keywords_zh"], eff["keywords_ja"], eff["keywords_ko"]
    translate, reads, rank, shells_mode = eff["translate"], eff["reads"], eff["rank"], eff["shells"]
    _check_params(min_mcap_usd, min_avg_volume, max_out, budget_usd, l2_max, reads, read_offset, rank)
    if shells_mode not in _shells.MODES:
        raise ValueError(f"shells must be one of {', '.join(_shells.MODES)} (got {shells_mode!r})")
    sv, sv_path = resolve_sieve(cfg, idea, sieve)
    sieve_hint = None
    from . import calib
    if sv is not None:
        calib.render_rules(sv.get("rules") or [], sv.get("facets"))    # ValueError on an unknown rule id
    elif sieve == "auto":
        sieve_hint = calib.closest_sieve(cfg, idea)       # a reworded idea: the closest sieve, for --sieve PATH
    t0 = time.monotonic()
    started = store.now_utc()
    run_id = f"scr-{started.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    factory = jev_factory or _run_factory(cfg)
    out_path = Path(out_dir) if out_dir is not None else _default_out_dir(cfg, idea, started)
    params = {"min_mcap_usd": min_mcap_usd, "min_avg_volume": min_avg_volume, "countries": countries,
              "max_out": max_out, "budget_usd": budget_usd, "l2_max": l2_max, "keywords": keywords,
              "dry_run": dry_run, "l1_adjacent_min": l1_adjacent_min, "l1_core_min": l1_core_min,
              "retry_uncertain": retry_uncertain, "keywords_zh": keywords_zh, "keywords_ja": keywords_ja,
              "keywords_ko": keywords_ko, "translate": translate, "reads": reads, "read_offset": read_offset,
              "from_run": from_run or None, "l1_new": bool(l1_new), "rank": rank,
              "sieve_path": (str(sv_path) if sv_path else "<inline>" if sv is not None else None),
              "sieve_version": (sv or {}).get("version"), "sieve_sha256": _sieve_sha(sv, sv_path),
              "shells": shells_mode, "shells_version": _shells.SHELLS_VERSION}
    if supersedes is not None:
        # the update pass runs on a small cap; a later from_run of this run inherits the user's own budget
        params.update(budget_usd=inherited.get("budget_usd", SCREEN_DEFAULTS["budget_usd"]),
                      update_budget_usd=budget_usd)
    notes: list[str] = []
    timing: dict[str, float] = {}

    # 1) universe (one short read-only session)
    with store.session(cfg, read_only=True, wait_s=READ_WAIT_S) as con:
        ucounts = universe_counts(con, min_mcap_usd=min_mcap_usd)
        known = known_country_names(con) if countries else set()
        rows = load_universe(con, min_mcap_usd=min_mcap_usd, min_avg_volume=None)
        descs = load_descriptions(con, min_mcap_usd=min_mcap_usd)
        docs = load_documents(con, min_mcap_usd=min_mcap_usd)
        names = calib.load_names(con) if (sv or {}).get("rules") or idea_en else None
        frozen = _frozen_idea_en(con, idea) if (sv or {}).get("idea_en") else (False, None)
        facts = _shells.load_facts(con, min_mcap_usd=min_mcap_usd)
        st = _shells.st_list(con, started.date())
        all_rows = load_universe_all(con)
    params["st_list"] = st.meta() if st is not None else None
    match = country_matcher(countries, known)       # ValueError on an unknown token: nothing sent or written
    tk = time.monotonic()
    from . import sieve_author
    author = sieve_author.author_keywords(sv, idea, frozen=frozen)
    if (author or {}).get("idea_en") and not idea_en:      # an explicit idea_en is used instead (and checked below)
        bad = sieve_author.idea_en_errors(author["idea_en"], sv)
        if bad:        # isolation: the question never names a company the sieve lists
            raise ValueError(f"sieve {sv_path or '<inline>'}: " + "；".join(b["text_zh"] for b in bad)
                             + "（jevscreen sieve check 可以检查），没有发送任何请求")
    kinfo = resolve_keywords(cfg, idea, keywords=keywords,
                             keywords_by_lang={"zh": keywords_zh, "ja": keywords_ja, "ko": keywords_ko},
                             translate=translate, keywords_fn=keywords_fn,
                             sieve_kw=_sieve_keywords(sv), author=author, idea_en=idea_en)
    kinfo["author_warnings"] = list((author or {}).get("warnings") or [])
    if kinfo["status"] == "sieve" and frozen[0] and kinfo.get("idea_en") != frozen[1]:
        kinfo["author_warnings"].append("比上次贵，因为 idea_en 改了：第一步和第二步会按新的 idea_en 重问一遍 / idea_en "
                                         "changed (--reprice): steps 1 and 2 are asked again")
    timing["keywords_s"] = round(time.monotonic() - tk, 2)
    params["idea_en_source"] = {"sieve": "sieve", "generated": "model", "frozen": "frozen",
                                "agent": "agent"}.get(kinfo["status"], "none")
    params["l2_question_sha"] = question_sha(build_l2_question(idea, kinfo.get("idea_en"), rules=(sv or {}).get(
        "rules") or (), facets=(sv or {}).get("facets")))
    params["sieve_terms_sha"] = sieve_terms_sha(sv)
    if names is not None and idea_en:
        # the same isolation for the English sentence every question carries (quickstart / screen --idea-en, a
        # --from-run base): no company name or ticker the idea itself does not name
        bad_en = [x for x in calib.idea_en_problems(idea_en, idea, names[0], names[1]) if x.startswith("names ")]
        if bad_en:
            raise ValueError(f"idea_en {str(idea_en)[:80]!r} {bad_en[0]}; write it without them (nothing was sent)")
    if names is not None and (sv or {}).get("rules"):
        # isolation, enforced where the question is built: the sieve's rule texts (rendered with its facets, which
        # the user's AI may have edited after adoption) never carry a company name, ticker or card quote
        bad = calib.rule_text_problems(sv["rules"], sv.get("facets"), names=names[0], tickers=names[1],
                                       terms=calib.keyword_terms(kinfo["terms"], sv))
        if bad:
            raise ValueError(f"sieve {sv_path or '<inline>'}: " + "；".join(
                f"规则 {rid}（{label}）的文字{'、'.join(probs)}" for rid, label, probs in bad)
                + "，不能进入 Jev 问题：改掉 facets / 规则（jevscreen sieve check 可以检查），没有发送任何请求")
    vol_ok = [r for r in rows if min_avg_volume is None
              or (r["avg_volume_10d"] is not None and r["avg_volume_10d"] >= min_avg_volume)]
    universe = [r for r in vol_ok if match(r["country"], r["exchange"])]
    ucounts.update(below_min_volume=len(rows) - len(vol_ok), other_country=len(vol_ok) - len(universe))
    # shells filter (free, before L1): drop blank-check companies, mark ST / *ST; never a company the sieve names
    hits = _shells.classify(universe, descs, facts, st)
    protected = calib.named_keys(sv)
    drops = _shells.effective_drops(hits, shells_mode, protected)
    if drops:
        ucounts["shells_dropped"] = len(drops)
    described, no_desc = [], []
    for r in universe:
        if r["company_key"] in drops:
            continue
        d = select_description(descs.get(r["company_key"], []))
        if d is None:
            no_desc.append(r)
        else:
            described.append({**r, "desc": d})
    stage = _pre_l1_stages(all_rows, min_mcap_usd, rows, vol_ok, universe, drops, no_desc)
    extras = _forced_extras(cfg, sv, described, stage, docs, notes)
    pre = {"all_rows": all_rows, "stage": stage, "hits": hits, "drops": drops, "extras": extras, "st": st,
           "mode": shells_mode, "protected": protected, "china": any(r.get("country") == "China" for r in universe)}
    timing["universe_s"] = round(time.monotonic() - t0 - timing["keywords_s"], 2)

    if not dry_run:
        _db_write(cfg, lambda con: store.upsert_many(con, "screen_runs", RUN_COLS, [(
            run_id, idea, json.dumps({**params, "idea_en": kinfo.get("idea_en"), "keywords_status": kinfo["status"]},
                                     ensure_ascii=False, sort_keys=True), started, None, "running",
            len(universe), None, None, None, None, None, str(out_path), None)]), notes, "screen_runs start")

    clients: dict[str, Any] = {"l1": None, "l2": None}
    try:
        return _screen_layers(cfg, idea, run_id=run_id, factory=factory, out_path=out_path, params=params,
                              notes=notes, timing=timing, t0=t0, started=started, ucounts=ucounts,
                              universe=universe, described=described, no_desc=no_desc, docs=docs, keywords=keywords,
                              kinfo=kinfo,
                              dry_run=dry_run, budget_usd=budget_usd, l2_max=l2_max, max_out=max_out,
                              l1_adjacent_min=l1_adjacent_min, l1_core_min=l1_core_min,
                              retry_uncertain=retry_uncertain, clients=clients, reads=reads, read_offset=read_offset,
                              rank=rank, base=base, sieve=sv, sieve_path=sv_path, sieve_hint=sieve_hint, pre=pre,
                              supersedes=supersedes, fetch_info=fetch_info, l1_new=l1_new,
                              progress=progress)
    except BaseException as e:
        # Ctrl-C / SIGTERM / a crash: never leave screen_runs 'running' without the money already spent.
        if not dry_run:
            cost = round(sum(_client_stats(c)[0] for c in clients.values()), 6)
            status = "interrupted" if isinstance(e, KeyboardInterrupt) else "error"
            with contextlib.suppress(Exception):
                _db_write(cfg, lambda con: con.execute(
                    "UPDATE screen_runs SET finished_at=?, status=?, cost_usd=?, note=? WHERE run_id=?",
                    [store.now_utc(), status, cost, f"{type(e).__name__}"[:200], run_id]), [], "screen_runs abort")
        raise


def question_sha(q) -> str:
    """sha256 (16 hex) of a Jev question as results.json stores it: a different sha means different L2 item keys."""
    body = json.dumps(_question_dict(q), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def sieve_terms_sha(sv: dict[str, Any] | None) -> str | None:
    """sha256 (16 hex) of what a sieve adds to the L2 excerpt terms (seed_terms, keywords add / weak), None when it
    adds nothing (or there is no sieve): a different value means some annual-report excerpts (and so their L2 item
    keys) may change."""
    if sv is None:
        return None
    kw = _sieve_keywords(sv) or {}
    seed = {lang: _clean_terms(v) for lang, v in sorted((sv.get("seed_terms") or {}).items()) if _clean_terms(v)}
    kws = {lang: {"add": _clean_terms(k.get("add")), "weak": _clean_terms(k.get("weak"))}
           for lang, k in sorted(kw.items()) if isinstance(k, dict) and (k.get("add") or k.get("weak"))}
    if not seed and not kws:
        return None
    body = {"seed": seed, "kw": kws}
    raw = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _frozen_idea_en(con, idea: str) -> tuple[bool, str | None]:
    """(a paid run of this idea exists, the idea_en it used): the newest screen_runs row of the idea (dry runs are
    never stored there)."""
    for (pj,) in con.execute("SELECT params_json FROM screen_runs WHERE trim(idea) = ? ORDER BY started_at DESC",
                             [idea.strip()]).fetchall():
        try:
            p = json.loads(pj or "{}")
        except ValueError:
            continue
        if isinstance(p, dict) and not p.get("dry_run"):
            return True, p.get("idea_en")
    return False, None


def _sieve_sha(sv: dict[str, Any] | None, path: Path | None) -> str | None:
    if sv is None:
        return None
    from . import calib
    return calib.sieve_sha256(sv, path)


def _sieve_keywords(sv: dict[str, Any] | None) -> dict[str, dict[str, list[str]]] | None:
    if sv is None:
        return None
    from . import calib
    return calib.keywords_of(sv)


def _band_estimate(est: dict[str, Any] | None, n_items: int, extra_reads: int, *,
                   first_reads: dict[str, float] | None = None, keys: Iterable[str] = (),
                   base_run: str | None = None) -> dict[str, Any] | None:
    """A dry-run L2 estimate with the band reads added (cost, reservation, requests and time scaled by the same
    factor). With `first_reads` (the base run's read-0 p_pos per company, from_run) the band is the real one: the
    `keys` whose read 0 lay in L2_BAND, plus BAND_SHARE_ESTIMATE of the keys that run did not read, never less than BAND_SHARE_ESTIMATE of all (a
    changed question can move items into the band); otherwise about BAND_SHARE_ESTIMATE of the items. band_reads says
    which ('run' / 'share')."""
    if not est or "error" in est or extra_reads <= 0:
        return est
    keys = list(keys)
    if first_reads and keys:
        known = [k for k in keys if k in first_reads]
        in_band = sum(1 for k in known if L2_BAND[0] <= first_reads[k] <= L2_BAND[1])
        band_n = in_band + BAND_SHARE_ESTIMATE * (len(keys) - len(known))
        basis = "run"
        text = (f"band reads: {in_band} of {len(known)} L2 items were in the band in run {base_run}"
                + (f" (+ about {BAND_SHARE_ESTIMATE:.0%} of {len(keys) - len(known)} items it did not read)"
                   if len(keys) > len(known) else ""))
        if band_n < BAND_SHARE_ESTIMATE * len(keys):     # a changed question can move items into the band: never
            band_n = BAND_SHARE_ESTIMATE * len(keys)     # estimate below the usual share
            text += f"; counted as the usual {BAND_SHARE_ESTIMATE:.0%} (upper bound)"
        text += f" x {extra_reads} more reads"
        share = band_n / len(keys)
    else:
        share, basis = BAND_SHARE_ESTIMATE, "share"
        band_n = share * n_items
        text = f"band reads: about {BAND_SHARE_ESTIMATE:.0%} of the items x {extra_reads} more reads"
    f = share * extra_reads
    out = dict(est)
    for k in ("est_cost_usd", "est_reserved_usd", "est_seconds"):
        if isinstance(out.get(k), (int, float)):
            out[k] = round(out[k] * (1 + f), 6)
    if isinstance(out.get("requests"), int):
        out["requests"] = out["requests"] + math.ceil(out["requests"] * f)
    out["band_reads"] = {"share": round(share, 4), "extra_reads": extra_reads, "items": round(band_n),
                         "est_items": round(band_n * extra_reads), "basis": basis, "text": text}
    out["basis"] = f"{out.get('basis') or ''}; + {text}"
    return out


def _screen_layers(cfg, idea: str, *, run_id, factory, out_path, params, notes, timing, t0, started, ucounts,
                   universe, described, no_desc, docs, keywords, dry_run, budget_usd, l2_max, max_out,
                   l1_adjacent_min, l1_core_min, retry_uncertain, clients, kinfo=None, reads=1, read_offset=0,
                   rank="label", base=None, sieve=None, sieve_path=None, sieve_hint=None, pre=None,
                   supersedes=None, fetch_info=None, progress=None, l1_new=False) -> dict[str, Any]:
    _, Item = _jev_types()
    pre = pre or {}
    hits: dict[str, dict] = pre.get("hits") or {}
    kinfo = kinfo or resolve_keywords(cfg, idea, keywords=keywords, keywords_by_lang=None, translate=False,
                                      keywords_fn=None)
    idea_en = kinfo.get("idea_en")
    sv = sieve
    q1 = build_l1_question(idea, idea_en)
    q2 = build_l2_question(idea, idea_en, rules=(sv or {}).get("rules") or (), facets=(sv or {}).get("facets"))
    terms_by_lang = kinfo["terms"]
    weak_by_lang = kinfo.get("weak") or {}
    terms = terms_by_lang["en"]
    warnings = []
    if kinfo["status"] == "unavailable":
        warnings.append(f"Automatic translation of the idea is unavailable ({kinfo.get('error')}): the Jev questions "
                        "use the idea verbatim and English excerpts use only its Latin words. Run `jevscreen keywords "
                        "\"<idea>\"` to check the local model, or pass --keywords.")
    warnings += keyword_warnings(idea, keywords, terms, translated=kinfo["status"] in ("generated", "sieve")
                                 or (kinfo["status"] == "frozen" and bool(kinfo.get("idea_en"))))
    warnings += kinfo.get("author_warnings") or []
    l1_items = [Item(item_id=c["company_key"], issuer=c["name"] or c["security_id"], text=c["desc"]["text"],
                     meta={"security_id": c["security_id"], "input_tier": c["desc"]["tier"],
                           "sources": c["desc"]["sources"]}) for c in described]
    by_key = {c["company_key"]: c for c in described}
    described_keys = set(by_key)
    extras = list(pre.get("extras") or [])
    by_key.update({c["company_key"]: c for c in extras})
    today = started.date()
    extra_idx = band_read_indices(reads, read_offset)

    # sieve: pinned companies and the user's AI's checks are always read by L2 (l2_forced)
    pins: dict[str, dict] = {}
    forced_cs: list[dict] = []
    if sv is not None:
        from . import calib
        pins = calib.pins(sv)
        by_sid = {c["security_id"]: c for c in described}
        for ex in calib.forced_examples(sv):
            c = by_key.get(ex.get("company_key")) or by_sid.get(ex.get("security_id"))
            if c is not None and c not in forced_cs:
                forced_cs.append(c)
        forced_cs += [c for c in extras if c not in forced_cs]     # outside the run's filters: read, never L1

    def l2_item(c: dict, inp: dict):
        return Item(item_id=c["company_key"], issuer=c["name"] or c["security_id"], text=inp["text"],
                    meta={"security_id": c["security_id"], "input_tier": inp["input_tier"],
                          "input_source": inp["input_source"]})

    def tell(phase: str, done: int, total: int) -> None:
        if progress is not None:
            with contextlib.suppress(Exception):     # a progress callback must never stop a paid run
                progress(phase, done, total)

    def make(layer: str, budget: float, dry: bool):
        kw: dict[str, Any] = {"run_id": run_id, "layer": layer, "budget_usd": budget, "dry_run": dry}
        if retry_uncertain and not dry:
            kw["retry_uncertain"] = True
        client = factory(cfg, **kw)
        if progress is not None and not dry and client is not None:
            with contextlib.suppress(Exception):     # JevClient heartbeat (a fake client may not take attributes)
                client.on_packet = lambda d, t, _layer=layer: tell(_layer, d, t)
        return client

    status = STATUS_OK
    errors: list[str] = []
    layers: dict[str, dict[str, Any]] = {"l1": {}, "l2": {}}
    l1_res: dict[str, dict] = {}
    l2_res: dict[str, dict] = {}
    l2_agg: dict[str, dict] = {}
    l2_inputs: dict[str, dict] = {}
    passes: list[dict] = []
    l2_sel: list[dict] = []
    l2_overflow: list[dict] = []
    forced_keys: list[str] = []
    band: dict[str, Any] | None = None
    dry_budget: dict[str, Any] | None = None
    sp_info: dict[str, dict[str, Any]] = {}

    def factory_error(layer: str, e: BaseException) -> str:
        if not _is_error(e, "JevUnavailable"):
            raise e
        errors.append(f"{layer}: {type(e).__name__}: {e}")
        return STATUS_BUSY if _is_error(e, "JevBusy") else STATUS_UNAVAILABLE

    def l1_passes_sorted() -> list[dict]:
        ps = [by_key[k] for k, r in l1_res.items()
              if k in by_key and l1_passes(r, adjacent_min=l1_adjacent_min, core_min=l1_core_min)]
        ps.sort(key=lambda c: (-(p_core_of(l1_res[c["company_key"]]) or 0.0), -(c["market_cap_usd"] or 0),
                               c["security_id"]))
        return ps

    # 2) L1 (from_run: the base run's stored answers, no Jev call; with l1_new only the newly described are read)
    t1 = time.monotonic()
    l1_client = None
    l1_send = l1_items
    if base is not None:
        l1_res = {k: dict(r) for k, r in base["l1"].items() if k in described_keys}
        missing = sum(1 for k in described_keys if k not in l1_res)
        layers["l1"].update(from_run=base["run_id"], loaded=len(l1_res))
        if l1_new and missing:
            l1_send = [it for it in l1_items if it.item_id not in l1_res]
            layers["l1"]["new"] = len(l1_send)
            try:
                clients["l1"] = make("l1", budget_usd, dry_run)
            except Exception as e:  # noqa: BLE001
                status = factory_error("l1", e)
            l1_client = clients["l1"]
            layers["l1"]["estimate"] = _estimate(l1_client, l1_send, q1) if l1_client is not None else None
            notes.append(f"L1 loaded from run {base['run_id']}: {len(l1_res)} answers, $0 (never re-read); "
                         f"{len(l1_send)} newly described companies read by L1 now (l1_new).")
        else:
            layers["l1"]["estimate"] = {"requests": 0, "items": 0, "est_input_tokens": 0, "est_cost_usd": 0.0,
                                        "est_reserved_usd": 0.0, "est_seconds": 0.0,
                                        "basis": f"L1 answers loaded from run {base['run_id']} (no Jev call)"}
            notes.append(f"L1 loaded from run {base['run_id']}: {len(l1_res)} answers, $0 (L1 is never re-read with "
                         "--from-run)." + (f" {missing} described companies have no L1 answer in that run and are "
                                           "treated as not passing L1 (l1_new reads them)." if missing else ""))
    else:
        try:
            clients["l1"] = make("l1", budget_usd, dry_run)
        except Exception as e:  # noqa: BLE001
            status = factory_error("l1", e)
        l1_client = clients["l1"]
        layers["l1"]["estimate"] = _estimate(l1_client, l1_items, q1) if l1_client is not None else None

    if dry_run:
        # L2 upper bound: the top l2_max described companies by market cap stand in for the (unknown) L1 passes
        # (from_run: the real passes of the base run's L1 answers).
        hypo = l1_passes_sorted()[:max(0, l2_max)] if base is not None else \
            sorted(described, key=lambda c: -(c["market_cap_usd"] or 0))[:max(0, l2_max)]
        hypo += [c for c in forced_cs if c not in hypo]
        hypo_items = []
        for c in hypo:
            inp = _l2_input(c, docs.get(c["company_key"]), terms_by_lang, today, weak_by_lang)
            l2_inputs[c["company_key"]] = inp
            hypo_items.append(Item(item_id=c["company_key"], issuer=c["name"] or c["security_id"], text=inp["text"],
                                   meta={"security_id": c["security_id"], "input_tier": inp["input_tier"]}))
        est2 = None
        if status == STATUS_OK:
            try:
                clients["l2"] = make("l2", budget_usd, True)
                est2 = _estimate(clients["l2"], hypo_items, q2)
            except Exception as e:  # noqa: BLE001
                factory_error("l2", e)
        if est2 is not None:
            est2["basis"] = (f"{est2.get('basis') or ''}; upper bound on the number of companies: "
                             + (f"the L1 passes of run {base['run_id']}" if base is not None else
                                f"the top {len(hypo_items)} described companies by market cap stand in for the L1 "
                                "passes"))
            est2 = _band_estimate(est2, len(hypo_items), len(extra_idx),
                                  first_reads=(base or {}).get("l2_p0"), keys=[it.item_id for it in hypo_items],
                                  base_run=(base or {}).get("run_id"))
            sp_items = [it for it in hypo_items if it.item_id in _should_pass_wants(sv, by_key)]
            if sp_items and est2 is not None and "error" not in est2:
                # upper bound: every should_pass company may come out low and get SHOULD_PASS_READS more reads
                e_sp = _estimate(clients["l2"], sp_items, q2) or {}
                add = float(e_sp.get("est_cost_usd") or 0.0) * SHOULD_PASS_READS
                est2 = {**est2, "est_cost_usd": round(float(est2.get("est_cost_usd") or 0.0) + add, 6),
                        "should_pass_reads": {"items": len(sp_items), "extra_reads": SHOULD_PASS_READS,
                                              "est_cost_usd": round(add, 6)}}
                if est2.get("est_reserved_usd") is not None:
                    est2["est_reserved_usd"] = round(float(est2["est_reserved_usd"]) + add, 6)
        layers["l2"]["estimate"] = est2
        layers["l2"]["inputs"] = len(hypo_items)
        e1 = layers["l1"]["estimate"] or {}
        e2 = est2 or {}
        est_cost = float(e1.get("est_cost_usd") or 0) + float(e2.get("est_cost_usd") or 0)
        est_res = (float(e1.get("est_reserved_usd") or e1.get("est_cost_usd") or 0)
                   + float(e2.get("est_reserved_usd") or e2.get("est_cost_usd") or 0))
        dry_budget = {"est_cost_usd": round(est_cost, 6), "est_reserved_usd": round(est_res, 6),
                      "budget_usd": budget_usd, "reservation_exceeds_budget": est_res > budget_usd,
                      "est_seconds": round(float(e1.get("est_seconds") or 0) + float(e2.get("est_seconds") or 0), 1)}
        if est_res > budget_usd:
            warnings.append(f"The estimated reservation ${est_res:.4f} exceeds the budget ${budget_usd:.4f}: "
                            "the run would stop early (budget_exhausted). Raise --budget or narrow the universe.")
        status = STATUS_DRY
    elif l1_client is not None:
        tell("l1", 0, len(l1_send))
        res, err_status, err = _call_classify(l1_client, l1_send, q1)
        if err_status:
            status = err_status
            errors.append(f"l1: {err}")
            spent, sent = _client_stats(l1_client)
            n_ok = sum(1 for r in res if r.get("status") == "ok")
            notes.append(f"L1 stopped after {sent} requests / ${spent:.4f} ({n_ok} answers kept in this report); "
                         "a rerun reuses the cached answers at no cost.")
        l1_res = {**l1_res, **{r["item_id"]: r for r in res}} if base is not None else {r["item_id"]: r for r in res}
        if any(r.get("status") == "skipped_budget" for r in res) and status == STATUS_OK:
            status = STATUS_BUDGET
    timing["l1_s"] = round(time.monotonic() - t1, 2)
    if not dry_run and l1_res:
        rows_l1 = [_result_row(run_id, by_key[k], "l1", r, None) for k, r in l1_res.items() if k in by_key]
        _db_write(cfg, lambda con: store.upsert_many(con, "screen_results", RESULT_COLS, rows_l1), notes,
                  "screen_results l1")

    # 3) L2 (read 0 of every selected company, then the band reads)
    t2 = time.monotonic()
    l2_items: list = []
    if not dry_run:
        passes = l1_passes_sorted()
        l2_sel, l2_overflow = passes[:max(0, l2_max)], passes[max(0, l2_max):]
        sel_keys = {c["company_key"] for c in l2_sel}
        forced_keys = [c["company_key"] for c in forced_cs if c["company_key"] not in sel_keys]
        l2_overflow = [c for c in l2_overflow if c["company_key"] not in forced_keys]
        for c in l2_sel + [by_key[k] for k in forced_keys]:
            inp = _l2_input(c, docs.get(c["company_key"]), terms_by_lang, today, weak_by_lang)
            if fetch_info is not None:
                inp["doc_fetch"] = _doc_fetch(c["company_key"], inp, fetch_info)
            l2_inputs[c["company_key"]] = inp
            l2_items.append(l2_item(c, inp))
        l1_spent, _ = _client_stats(l1_client)
        remaining = budget_usd - l1_spent
        layers["l2"]["budget_usd"] = round(max(0.0, remaining), 6)
        base_err = None
        if l2_items and status not in (STATUS_UNAVAILABLE, STATUS_BUSY, STATUS_BUDGET):
            if remaining <= 0:
                for it in l2_items:
                    l2_res[it.item_id] = _not_sent(it, "no budget left after L1", "skipped_budget")
            else:
                try:
                    clients["l2"] = make("l2", remaining, False)
                except Exception as e:  # noqa: BLE001
                    status = factory_error("l2", e)
                if clients["l2"] is not None:
                    layers["l2"]["estimate"] = _estimate(clients["l2"], l2_items, q2)
                    tell("l2", 0, len(l2_items))
                    res, err_status, err = _call_classify(clients["l2"], l2_items, q2)
                    base_err = err_status
                    if err_status in (STATUS_UNAVAILABLE, STATUS_BUSY):
                        status = err_status
                    if err:
                        errors.append(f"l2: {err}")
                    l2_res = {r["item_id"]: r for r in res}
        if extra_idx and l2_res:
            band = _l2_band_reads(clients["l2"] if base_err is None else None, l2_items, l2_res, q2, reads,
                                  read_offset)
            if band["error"]:
                errors.append(f"l2 band reads: {band['error']}")
            if band["skipped"] and status == STATUS_OK:
                status = STATUS_PARTIAL
        band_ids = set(band["band"]) if band else set()
        planned_n = (len(extra_idx) if read_offset > 0 else 1 + len(extra_idx)) if extra_idx else 1
        seqs: dict[str, list[tuple[int, dict]]] = {}
        for k, r in list(l2_res.items()):
            if r.get("status") != "ok":
                continue
            extra = band["reads"].get(k, []) if band else []
            seq = extra if (read_offset > 0 and k in band_ids) else [(0, r)] + extra
            agg = aggregate_reads(seq) or aggregate_reads([(0, r)] + extra)
            if agg is None:
                continue
            seqs[k] = seq if aggregate_reads(seq) else [(0, r)] + extra
            want = planned_n if k in band_ids else 1
            agg["edge"] = L2_EDGE[0] <= agg["p_pos"] < L2_EDGE[1]
            agg["note"] = None if agg["n"] >= want else READ_ONCE_NOTE if agg["n"] == 1 else \
                f"只读了 {agg['n']}/{want} 次"
            l2_agg[k] = agg
            if not (agg["n"] == 1 and [d["read"] for d in agg["reads"] if d["p_pos"] is not None] == [0]):
                l2_res[k] = {**r, "label": agg["label"], "probs": agg["probs"]}     # decided on the mean
        profile_keys = {k for k, inp in l2_inputs.items() if inp.get("evidence") == "profile"}

        def cap_profiles() -> None:
            # a profile is the same kind of text as L1: it can support 'partial', never 'explicit' (仅简介)
            for k, r in list(l2_res.items()):
                if r.get("status") == "ok" and r.get("label") == "explicit" and k in profile_keys:
                    l2_res[k] = {**r, "label": "partial", "label_capped": True}
        cap_profiles()                            # before the should_pass check: it judges the label shown
        if sv is not None and clients["l2"] is not None and base_err is None:
            sp_info = _should_pass_reads(clients["l2"], l2_items, l2_res, l2_agg, seqs, sv, by_key, q2,
                                         (base or {}).get("l2_labels") or {}, profile_keys=profile_keys)
            cap_profiles()                        # the extra reads decide on the mean: cap it again
            if any(x["extra_reads"] < x["planned"] for x in sp_info.values()) and status == STATUS_OK:
                status = STATUS_PARTIAL
        if band is not None:
            layers["l2"]["band"] = {"items": len(band_ids), "reads": extra_idx, "read_offset": read_offset,
                                    "planned": band["planned"], "ok": band["ok"], "skipped": band["skipped"],
                                    "cost_usd": band["cost_usd"],
                                    "edge": sum(1 for k in band_ids if (l2_agg.get(k) or {}).get("edge"))}
        if l2_res:
            rows_l2 = [_result_row(run_id, by_key[k], "l2", r, l2_inputs[k], l2_agg.get(k))
                       for k, r in l2_res.items() if k in by_key]
            _db_write(cfg, lambda con: store.upsert_many(con, "screen_results", RESULT_COLS_L2, rows_l2), notes,
                      "screen_results l2")
        if status == STATUS_OK and (l2_overflow or any(r.get("status") != "ok" for r in l2_res.values())
                                    or (l2_items and not l2_res)):
            status = STATUS_PARTIAL
    timing["l2_s"] = round(time.monotonic() - t2, 2)
    l2_client = clients["l2"]

    # 4) rank: only L2-verified companies; the rest of the L1 passes is 'unverified' (contradicted is dropped).
    # With a sieve, pins then apply (pin_and_rank): a forced company that missed L1 is ranked only when pinned.
    overflow_keys = {c["company_key"] for c in l2_overflow}
    pass_keys = {c["company_key"] for c in passes}
    cands = passes + [by_key[k] for k in forced_keys if k not in pass_keys and _pin_of(pins, by_key[k])
                      and by_key[k].get("extra_stage") in (None, "no_description")]
    for c in extras:
        if _pin_of(pins, c) and c.get("extra_stage") != "no_description":
            notes.append(f"校准：{c['name'] or c['security_id']} 在这次筛选条件以外（{STAGE_ZH.get(c['extra_stage'])}）："
                         "只读证据写进报告「你关心的公司」，不进名单")
    verified, unverified, contradicted = [], [], []
    for c in cands:
        k = c["company_key"]
        r1, r2, inp = l1_res.get(k), l2_res.get(k), l2_inputs.get(k)
        l2_ok = bool(r2) and r2.get("status") == "ok"
        l2_label = r2.get("label") if l2_ok else None
        if l2_ok:
            l2_status = l2_label
        elif r2:
            l2_status = r2.get("status") or "failed"
        else:
            l2_status = "not_sent_l2_max" if k in overflow_keys else "not_run"
        pc = p_core_of(r1)
        evidence = inp.get("evidence") if (inp and l2_ok) else None
        agg = l2_agg.get(k) if l2_ok else None
        if rank == "ev":
            score = score_ev((agg or {}).get("p_explicit"), (agg or {}).get("p_partial"), pc, c["market_cap_usd"],
                             evidence)
        else:
            score = score_of(l2_label, pc, c["market_cap_usd"], evidence)
        entry = {"company_key": k, "security_id": c["security_id"], "name": c["name"],
                 "market_cap_usd": c["market_cap_usd"], "l1_p_core": pc, "l2_label": l2_label,
                 "l2_evidence": evidence, "score": score,
                 "_x": (c, r1, r2, inp if l2_ok else None, l2_status, agg, k in forced_keys)}
        if l2_label in L2_VERIFIED:
            verified.append(entry)
        elif l2_label == "contradicted" and not _pin_of(pins, c):
            contradicted.append(entry)
        else:
            unverified.append(entry)
    excluded: list[dict] = []
    if sv is not None:
        verified, unverified, excluded, pin_notes = pin_and_rank(verified, unverified, sv, max_out)
        notes += pin_notes
    else:
        for e in verified:
            e.update(user_verdict=None, verdict_source="evidence", backfill=False)
        verified.sort(key=_order)
        unverified.sort(key=_order)
    ranked = output_entries(verified, max_out)
    top, unv_top = [e for _, e in ranked], unverified[:max_out]
    fund: dict[str, dict] = {}
    need = [e["security_id"] for e in top + unv_top]
    if need:
        try:
            with store.session(cfg, read_only=True, wait_s=READ_WAIT_S) as con:
                fund = load_fundamentals(con, need)
        except store.StoreLocked as e:
            notes.append(f"fundamentals: store locked ({str(e)[:120]}); revenue and CAGR left NULL")
            if status == STATUS_OK:
                status = STATUS_PARTIAL

    def out_row(i: int | None, e: dict[str, Any]) -> dict[str, Any]:
        c, r1, r2, inp, l2_status, agg, forced = e["_x"]
        l2_label, pc = e["l2_label"], e["l1_p_core"]
        f = fund.get(c["security_id"], {})
        p2 = (r2 or {}).get("probs") or {}
        inp = inp or {}          # set only when L2 judged the text (status ok): never show unjudged text as evidence
        agg = agg or {}
        row = {
            "rank": i, "security_id": c["security_id"], "company_key": c["company_key"], "name": c["name"],
            "country": c["country"], "region": coverage.region_for(c["country"], c["exchange"]),
            "market_cap_usd": c["market_cap_usd"], "revenue_ttm_usd": f.get("revenue_ttm_usd"),
            "revenue_cagr_3y_local": f.get("cagr"), "cagr_currency": f.get("cagr_currency"),
            "cagr_years": f.get("cagr_years"), "l1_label": (r1 or {}).get("label"), "l1_p_core": pc,
            "l2_label": l2_label, "l2_p_top": max(p2.values()) if (p2 and l2_label) else None,
            "l2_status": l2_status, "l2_evidence": inp.get("evidence"),
            "score": round(e["score"], 4),
            "evidence_excerpt": inp.get("evidence_excerpt"), "evidence_url": inp.get("evidence_url"),
            "filing_source": inp.get("source_id"), "filing_form": inp.get("form"),
            "filing_date": inp.get("filing_date"), "doc_lang": inp.get("lang"),
            "l2_doc_stale": inp.get("stale") if inp else None, "l2_keyword_hit": inp.get("keyword_hit"),
            "l1_input_tier": c["desc"]["tier"], "l2_input_tier": inp.get("input_tier"),
            "l2_input_source": inp.get("input_source"),
            "l1_request_id": (r1 or {}).get("request_id"), "l2_request_id": (r2 or {}).get("request_id"),
            # repeated reads (stability) and calibration
            "l2_reads": agg.get("n"), "l2_p_pos": _r4(agg.get("p_pos")), "l2_p_pos_sd": _r4(agg.get("p_pos_sd")),
            "l2_edge": agg.get("edge"), "user_verdict": e.get("user_verdict"),
            "verdict_source": e.get("verdict_source"), "backfill": e.get("backfill"),
            "l2_p_explicit": _r4(agg.get("p_explicit")), "l2_p_partial": _r4(agg.get("p_partial")),
            "l2_read_note": agg.get("note"), "l2_read_detail": agg.get("reads"),
            "evidence_sha": inp.get("evidence_sha"), "l2_forced": forced,
            "l2_label_cap": PROFILE_CAP_ZH if (l2_label in L2_VERIFIED and inp.get("evidence") == "profile") else None,
            "l2_should_pass": sp_info.get(c["company_key"]),
            "doc_fetch": (l2_inputs.get(c["company_key"]) or {}).get("doc_fetch"),
        }
        for k in ("user_note", "user_chip", "below_cut", "user_pin_via", "user_pin_at"):
            if e.get(k) is not None:
                row[k] = e[k]
        fl = (hits.get(c["company_key"]) or {}).get("flags")
        if fl:
            row["flags"] = list(fl)
        return row

    out_rows = [out_row(i, x) for i, x in ranked]
    unv_rows = [out_row(None, x) for x in unv_top]
    excluded_rows = [out_row(None, x) for x in excluded]

    # layer stats
    for layer, client, res in (("l1", l1_client, l1_res), ("l2", l2_client, l2_res)):
        spent, sent = _client_stats(client) if not dry_run else (0.0, 0)
        layers[layer].update(cost_usd=round(spent, 6), requests=sent, items=len(res), by_status=_counts(res, "status"),
                             labels=_counts({k: r for k, r in res.items() if r.get("status") == "ok"}, "label"),
                             cache_hits=sum(1 for r in res.values() if r.get("cached")),
                             seconds=timing[f"{layer}_s"])
        if client is not None and not dry_run:
            unr = list(getattr(client, "uncertain_not_resent", None) or [])
            if unr:
                layers[layer]["uncertain_not_resent"] = unr
                notes.append(f"{layer.upper()}: {sum(u.get('items', 0) for u in unr)} items have an earlier send with "
                             f"an unknown outcome (runs {', '.join(sorted({str(u.get('previous_run_id')) for u in unr}))}"
                             "); not resent. Rerun with --retry-uncertain to resend them (they may be charged twice).")
            drift = getattr(client, "price_drift_ratio", None)
            if drift:
                warnings.append(f"{layer.upper()}: actual Jev cost ran {drift:.2f}x the calibrated estimate (price "
                                "change?). The budget still held; check the price before the next run.")
            for le in getattr(client, "ledger_errors", None) or []:
                notes.append(f"{layer.upper()} ledger: {le}")
    layers["l1"]["inputs"] = len(l1_items)
    if not dry_run:
        layers["l2"]["inputs"] = len(l2_sel) + len(forced_keys)
        if forced_keys:
            layers["l2"]["forced"] = len(forced_keys)
    layers["l2"]["sec_inputs"] = sum(1 for i in l2_inputs.values() if i["evidence"] == "annual_report")
    layers["l2"]["profile_inputs"] = sum(1 for i in l2_inputs.values() if i["evidence"] == "profile")
    layers["l2"]["keyword_excerpts"] = sum(1 for i in l2_inputs.values() if i.get("keyword_hit"))
    layers["l2"]["inputs_by_source"] = _counts({k: i for k, i in l2_inputs.items() if i["evidence"] == "annual_report"},
                                               "source_id")
    layers["l2"]["inputs_by_lang"] = _counts({k: i for k, i in l2_inputs.items() if i["evidence"] == "annual_report"},
                                             "lang")
    layers["l2"]["summary_inputs"] = sum(1 for i in l2_inputs.values() if i.get("summary"))
    cost = round(layers["l1"]["cost_usd"] + layers["l2"]["cost_usd"], 6)

    def brief(c: dict, r: dict | None = None) -> dict:
        return {"security_id": c["security_id"], "name": c["name"], "market_cap_usd": c["market_cap_usd"],
                "status": (r or {}).get("status"), "error": (r or {}).get("error")}

    gaps = {
        "no_description": [brief(c) for c in no_desc],
        "l1_skipped_budget": [brief(by_key[k], r) for k, r in l1_res.items() if r.get("status") == "skipped_budget"],
        "l1_failed": [brief(by_key[k], r) for k, r in l1_res.items() if r.get("status") in ("failed", "uncertain")],
        "l2_not_sent_l2_max": [brief(c) for c in l2_overflow],
        "l2_skipped_budget": [brief(by_key[k], r) for k, r in l2_res.items() if r.get("status") == "skipped_budget"],
        "l2_failed": [brief(by_key[k], r) for k, r in l2_res.items() if r.get("status") in ("failed", "uncertain")],
        "l2_contradicted": [brief(x["_x"][0], x["_x"][2]) for x in contradicted],
        "l2_profile_only": [{**brief(by_key[k]), "note": "no annual report text; L2 reads the profile"}
                            for k, i in l2_inputs.items() if i["evidence"] == "profile"],
        "sec_text_fallback": [{**brief(by_key[k]), "note": i["note"]} for k, i in l2_inputs.items() if i.get("note")],
        "sec_document_old": [{**brief(by_key[k]), "note": f"{i.get('form') or 'filing'} filed {i.get('filing_date')}"}
                             for k, i in l2_inputs.items() if i.get("stale")],
    }
    if fetch_info is not None and not dry_run:
        layers["fetch"] = fetch_info.get("summary")
        oc = fetch_info.get("outcomes") or {}
        gaps["l2_doc_unavailable"] = [
            {"security_id": by_key[k]["security_id"], "name": by_key[k]["name"], "country": by_key[k]["country"],
             "exchange": by_key[k]["exchange"], "market_cap_usd": by_key[k]["market_cap_usd"],
             "source": (oc.get(k) or {}).get("source"), "reason": i["doc_fetch"], "note": (oc.get(k) or {}).get("note"),
             **{x: (oc.get(k) or {})[x] for x in ("date", "days", "cap", "when") if x in (oc.get(k) or {})}}
            for k, i in l2_inputs.items() if i["evidence"] == "profile"]
    funnel = {
        **ucounts, "universe": len(universe), "no_description": len(no_desc), "described": len(described),
        "l1_inputs": len(l1_items),
        "l1_sent": sum(1 for r in l1_res.values() if r.get("status") != "skipped_budget"),
        "l1_ok": sum(1 for r in l1_res.values() if r.get("status") == "ok"), "l1_pass": len(passes),
        "l2_inputs": layers["l2"].get("inputs", 0), "l2_sec_inputs": layers["l2"]["sec_inputs"],
        "l2_profile_inputs": layers["l2"]["profile_inputs"],
        "l2_sent": sum(1 for r in l2_res.values() if r.get("status") != "skipped_budget"),
        "l2_ok": sum(1 for r in l2_res.values() if r.get("status") == "ok"),
        "l2_verified": len(verified), "l2_contradicted": len(contradicted), "unverified": len(unverified),
        "output": len(out_rows),
    }
    if fetch_info is not None and not dry_run:
        summ = fetch_info.get("summary") or {}
        funnel.update(
            l2_doc_fetch_planned=sum((summ.get("planned") or {}).values()),
            l2_doc_fetched=sum(1 for i in l2_inputs.values() if i.get("doc_fetch") == "fetched_now"),
            l2_doc_deferred=sum(1 for i in l2_inputs.values() if i.get("doc_fetch") in ("deferred_time",
                                                                                          "deferred_cap")),
            l2_doc_unavailable=len(gaps["l2_doc_unavailable"]))
    if dry_run:
        tiers_used = sorted({c["desc"]["tier"] for c in described if c["desc"]["tier"]}
                            | {i["input_tier"] for i in l2_inputs.values() if i.get("input_tier")})
    else:
        tiers_used = sorted({t for r in out_rows + unv_rows + excluded_rows
                             for t in (r["l1_input_tier"], r["l2_input_tier"]) if t})
    timing["total_s"] = round(time.monotonic() - t0, 2)
    finished = store.now_utc()
    result = {
        "run_id": run_id, "idea": idea, "status": status, "dry_run": dry_run, "params": params,
        "started_at": started.isoformat(timespec="seconds"), "finished_at": finished.isoformat(timespec="seconds"),
        "funnel": funnel, "layers": layers, "cost_usd": cost, "budget_usd": budget_usd, "timing": timing,
        "questions": {"l1": _question_dict(q1), "l2": _question_dict(q2)}, "terms": terms,
        "terms_by_lang": terms_by_lang, "idea_en": idea_en,
        "keywords": {k: kinfo.get(k) for k in ("status", "idea_en", "model", "cached", "seconds", "error",
                                                "generated", "terms", "user_keywords")},
        "rows": out_rows, "unverified": unv_rows, "gaps": gaps, "tiers_used": tiers_used,
        "gray_private": "gray-private" in tiers_used, "errors": errors, "warnings": warnings, "notes": notes,
        "output_dir": str(out_path),
    }
    if excluded_rows or sv is not None:
        result["excluded_by_user"] = excluded_rows
    if sieve_hint is not None:
        result["sieve_hint"] = sieve_hint
    if sv is not None:
        result["calibration"] = _calibration_summary(sv, sieve_path, params, by_key, forced_keys, l1_res, l2_res,
                                                     passes, out_rows, excluded_rows, weak_by_lang, max_out,
                                                     should_pass=sp_info)
    if dry_budget is not None:
        result["dry_run_budget"] = dry_budget
    # shells filter summary, the ST warning, the sieve's companies read outside the filters
    drops = pre.get("drops") or set()
    if drops:
        gaps["shells_dropped"] = [{**brief(r), "rule": "+".join(hits[r["company_key"]]["drop"]),
                                   "pattern": (hits[r["company_key"]]["ev"].get("spac") or {}).get("pat"),
                                   "industry": hits[r["company_key"]].get("industry"),
                                   "revenue_ttm_usd": hits[r["company_key"]].get("revenue_ttm_usd")}
                                  for r in universe if r["company_key"] in drops]
    result["shells"] = _shells_summary(pre, described)
    st_rows = _shells.top_st_rows(out_rows)
    if st_rows:
        result["st_warning"] = {"security_ids": [r["security_id"] for r in st_rows],
                                "text_zh": _shells.ST_WARNING_ZH, "text_en": _shells.ST_WARNING_EN}
    if extras and "calibration" in result:
        result["calibration"]["extras"] = [
            {"security_id": c["security_id"], "company_key": c["company_key"], "name": c["name"],
             "stage": c["extra_stage"], "market_cap_usd": c["market_cap_usd"],
             "l2_label": (l2_res.get(c["company_key"]) or {}).get("label")
             if (l2_res.get(c["company_key"]) or {}).get("status") == "ok" else None,
             "l2_status": (l2_res.get(c["company_key"]) or {}).get("status"),
             "l2_evidence": (l2_inputs.get(c["company_key"]) or {}).get("evidence")} for c in extras]

    if supersedes is not None:
        result["supersedes"] = supersedes

    # 5) outputs + persist
    l2_lines = None if dry_run else [_l2_input_line(by_key[it.item_id], l2_inputs[it.item_id]) for it in l2_items]
    ledger = None
    if pre.get("all_rows") is not None:
        ledger = _ledger(result, pre, by_key, l1_res, l2_res, l2_agg, l2_inputs, base,
                         adjacent_min=l1_adjacent_min, core_min=l1_core_min)
    written = True
    if supersedes is not None:
        written = supersede_allowed(status, (base or {}).get("status"))
        result["outputs_written"] = written
        if written:
            write_outputs(result, out_path, l2_lines, ledger, atomic=True)
    else:
        write_outputs(result, out_path, l2_lines, ledger)
    if not dry_run:
        def fin(con):
            con.execute("UPDATE screen_runs SET finished_at=?, status=?, l1_n=?, l1_pass_n=?, l2_n=?, output_n=?, "
                        "cost_usd=?, note=?" + ("" if written else ", output_dir=NULL") + " WHERE run_id=?",
                        [finished, status, funnel["l1_sent"], funnel["l1_pass"], funnel["l2_sent"], funnel["output"],
                         cost, "; ".join(errors + warnings + notes)[:2000] or None, run_id])
        _db_write(cfg, fin, notes, "screen_runs finish")
    return result


def supersede_allowed(status: str, base_status: str | None) -> bool:
    """An update pass replaces the base run's files only when it is ok / partial and not worse (ok < partial)."""
    rank = {STATUS_OK: 0, STATUS_PARTIAL: 1}
    if status not in rank:
        return False
    return rank[status] <= rank.get(base_status or "", 1)


def _doc_fetch(ck: str, inp: dict[str, Any], fetch_info: dict[str, Any]) -> str:
    """'fetched_now' (read from a document the on-demand fetch just stored), 'stored', or the reason code the
    company still reads its profile (jevscreen.ondemand.REASONS)."""
    fetched = set(fetch_info.get("fetched") or ())
    if inp["evidence"] == "annual_report":
        return "fetched_now" if ck in fetched else "stored"
    o = (fetch_info.get("outcomes") or {}).get(ck) or {}
    code = o.get("code")
    if code == "fetched":      # stored now, yet not readable as excerpts (empty / unreadable text)
        return "extract_failed"
    if code == "stored_since":  # stored by an earlier fetch, yet still no usable excerpts
        return "already_stored"
    return code or "unresolved"


def _r4(v: float | None) -> float | None:
    return None if v is None else round(v, 4)


STAGE_ZH = {"null_mcap": "没有市值数据", "below_min_mcap": "市值低于门槛", "below_min_volume": "成交量低于门槛",
            "other_country": "不在所选国家/地区", "shell": "排除壳公司", "no_description": "没有公司简介"}
LEDGER_FORMAT = "jevscreen.funnel/1"
LEDGER_NAME = "funnel.jsonl.gz"


def _shells_summary(pre: dict[str, Any], described: list[dict]) -> dict[str, Any]:
    """result['shells']: mode, rule version, how many were dropped / kept because protected, the marks among the
    companies that reached L1, and the ST list used (a gap when missing or stale and the universe has A shares)."""
    hits, drops, st = pre.get("hits") or {}, pre.get("drops") or set(), pre.get("st")
    flags: dict[str, int] = {}
    for c in described:
        for f in (hits.get(c["company_key"]) or {}).get("flags") or []:
            flags[f] = flags.get(f, 0) + 1
    prot = set(pre.get("protected") or ())
    st_gap = None
    if pre.get("china"):
        if st is None:
            st_gap = {"zh": "ST 名单缺失：A 股的风险警示（ST/*ST）这次没有标出", "en": "ST list missing: A-share "
                      "risk warnings (ST / *ST) are not marked in this run"}
        elif st.stale:
            st_gap = {"zh": f"ST 名单日期 {str(st.fetched_at)[:10]}（超过 {_shells.ST_STALE_DAYS} 天）：标记可能过时",
                      "en": f"ST list dated {str(st.fetched_at)[:10]} (older than {_shells.ST_STALE_DAYS} days): "
                            "marks may be out of date"}
    return {"mode": pre.get("mode"), "version": _shells.SHELLS_VERSION, "dropped": len(drops),
            "kept_protected": sum(1 for k, h in hits.items()
                                  if h["drop"] and (k in prot or h.get("security_id") in prot)),
            "kept_mode": sum(1 for h in hits.values() if h["drop"]) if pre.get("mode") == "keep" else 0,
            "flags": dict(sorted(flags.items())), "st_list": st.meta() if st is not None else None,
            "st_gap": st_gap}


def _ledger(result: dict[str, Any], pre: dict[str, Any], by_key: dict[str, dict], l1_res: dict[str, dict],
            l2_res: dict[str, dict], l2_agg: dict[str, dict], l2_inputs: dict[str, dict], base: dict | None, *,
            adjacent_min: float, core_min: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(header, lines) of funnel.jsonl.gz: one line per universe company with the stage it stopped at.

    s: null_mcap | below_min_mcap | below_min_volume | other_country | shell | no_description | l1_sent (answered)
    | l1_loaded (answer taken from the --from-run base) | l1_not_sent (dry run, budget, failure); m: market cap in whole
    USD; r: the shells rules that matched (with 'kept': protected | keep when it was not dropped); f: marks; e: the
    evidence (pattern id + character offsets, never text); l1: {lab, p: [core, adjacent, unrelated, insufficient],
    ok (passes), src, st}; l2: {lab, st, pp (mean P(explicit)+P(partial)), ev, src}; x: the filter a forced sieve
    company failed (read by L2 anyway)."""
    from . import keywords
    hits, stage, drops = pre.get("hits") or {}, pre.get("stage") or {}, pre.get("drops") or set()
    extras = {c["company_key"]: c["extra_stage"] for c in pre.get("extras") or []}
    p = result["params"]
    as_of = next((r["market_as_of"] for r in pre["all_rows"] if r.get("market_as_of") is not None), None)
    header = {"format": LEDGER_FORMAT, "run_id": result["run_id"], "idea_key": keywords.idea_key(result["idea"]),
              "idea": result["idea"], "status": result["status"], "dry_run": bool(result.get("dry_run")),
              "started_at": result["started_at"], "market_as_of": str(as_of) if as_of is not None else None,
              "params": {k: p.get(k) for k in ("min_mcap_usd", "min_avg_volume", "countries", "shells",
                                               "l1_adjacent_min", "l1_core_min", "max_out", "l2_max", "from_run")},
              "shells_version": _shells.SHELLS_VERSION, "st_list": p.get("st_list"),
              "sieve_sha256": p.get("sieve_sha256")}
    lines = []
    for r in pre["all_rows"]:
        k = r["company_key"]
        st = stage.get(k, "null_mcap")
        ln: dict[str, Any] = {"k": k, "id": r["security_id"], "n": r["name"],
                              "m": int(round(r["market_cap_usd"])) if r["market_cap_usd"] is not None else None}
        if st == "described":
            x = l1_res.get(k)
            st = "l1_not_sent" if not x or x.get("status") != "ok" else "l1_loaded" if base is not None else "l1_sent"
            if x:
                pr = x.get("probs") or {}
                ln["l1"] = {"lab": x.get("label"), "p": [_r4(pr.get(n)) for n in ("core", "adjacent", "unrelated",
                                                                                   "insufficient")],
                            "ok": l1_passes(x, adjacent_min=adjacent_min, core_min=core_min),
                            "src": "+".join((by_key.get(k) or {}).get("desc", {}).get("sources") or []) or None}
                if x.get("status") != "ok":
                    ln["l1"]["st"] = x.get("status")
        ln["s"] = st
        h = hits.get(k)
        if h:
            if h["drop"]:
                ln["r"] = list(h["drop"])
                if k not in drops:
                    ln["kept"] = "keep" if pre.get("mode") == "keep" else "protected"
            fl = [f for f in h["flags"] if f not in h["drop"]]
            if fl:
                ln["f"] = fl
            if h["ev"]:
                ln["e"] = h["ev"]
        if k in extras:
            ln["x"] = extras[k]
        y = l2_res.get(k)
        if y:
            ok = y.get("status") == "ok"
            inp = l2_inputs.get(k) or {}
            ln["l2"] = {"lab": y.get("label") if ok else None, "st": y.get("status"),
                        "pp": _r4((l2_agg.get(k) or {}).get("p_pos") if ok and k in l2_agg
                                  else p_pos_of(y.get("probs")) if ok else None),
                        "ev": inp.get("evidence"), "src": inp.get("input_source")}
        lines.append(ln)
    return header, lines


def read_ledger(path: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """(header, lines) of a funnel.jsonl.gz (a file or a run's output directory); None when it is missing."""
    import gzip
    p = Path(path)
    if p.is_dir():
        p = p / LEDGER_NAME
    try:
        raw = gzip.decompress(p.read_bytes()).decode("utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    rows = [json.loads(x) for x in raw.splitlines() if x.strip()]
    if not rows or rows[0].get("format") != LEDGER_FORMAT:
        return None
    return rows[0], rows[1:]


def read_ledger_header(path: str | Path) -> dict[str, Any] | None:
    """Only the header line of a funnel.jsonl.gz (cheap: the first line of the stream)."""
    import gzip
    p = Path(path)
    if p.is_dir():
        p = p / LEDGER_NAME
    try:
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            h = json.loads(fh.readline() or "null")
    except (OSError, ValueError, EOFError):
        return None
    return h if isinstance(h, dict) and h.get("format") == LEDGER_FORMAT else None


def _l2_input_line(c: dict[str, Any], inp: dict[str, Any]) -> dict[str, Any]:
    """One line of l2_inputs.jsonl: the exact text layer 2 read for a company and what it was built from."""
    line = {"company_key": c["company_key"], "security_id": c["security_id"], "evidence": inp["evidence"],
            "source_id": inp.get("source_id"), "lang": inp.get("lang"), "text": inp["text"],
            "excerpts": inp.get("excerpts"), "keyword_hit": inp.get("keyword_hit"),
            "matched_terms": inp.get("matched_terms") or [], "evidence_sha": inp.get("evidence_sha")}
    if inp.get("doc_fetch") is not None:
        line["doc_fetch"] = inp["doc_fetch"]
    return line


SHOULD_PASS_READS = 2          # extra L2 reads of a should_pass company that came out below its level
_LEVEL = {"explicit": 2, "partial": 1}
_LEVEL_ZH = {"explicit": "明确符合", "partial": "相关"}


def should_pass_flag_zh(sp: dict[str, Any]) -> str:
    """'应通过：多读 2 次后通过' / '应通过：多读 2 次，仍低于「明确符合」' / '应通过：没能多读（预算），仍低于「相关」';
    a profile-only company wanted explicit: '应通过：仅简介，最多算相关' or the above with （仅简介，最多算相关）."""
    n = sp.get("extra_reads") or 0
    cap = f"{PROFILE_CAP_ZH}，最多算相关" if sp.get("capped") else ""
    if sp.get("held") and not n and not sp.get("planned") and cap:
        return f"应通过：{cap}"
    how = f"多读 {n} 次" if n else "没能多读（预算）"
    tail = f"（{cap}）" if cap else ""
    if sp.get("held"):
        return f"应通过：{how}后通过{tail}"
    want = sp.get("want_eff") or sp.get("want")
    return f"应通过：{how}，仍低于「{_LEVEL_ZH.get(want, want)}」{tail}"


def _should_pass_wants(sv: dict[str, Any] | None, by_key: dict[str, dict]) -> dict[str, str]:
    """{company_key: want} of the sieve's should_pass checks (source 'sieve', want explicit / partial) found in
    by_key."""
    if not sv:
        return {}
    from . import calib
    by_sid = {c["security_id"]: c for c in by_key.values()}
    want_of: dict[str, str] = {}
    for ex in calib.forced_examples(sv):
        if ex.get("source") != "sieve" or ex.get("want") not in _LEVEL:
            continue
        c = by_key.get(ex.get("company_key")) or by_sid.get(ex.get("security_id"))
        if c is not None:
            want_of[c["company_key"]] = ex["want"]
    return want_of


def _should_pass_reads(client, l2_items: list, l2_res: dict[str, dict], l2_agg: dict[str, dict],
                       seqs: dict[str, list[tuple[int, dict]]], sv: dict[str, Any], by_key: dict[str, dict], q2,
                       base_labels: dict[str, str], *, profile_keys: set[str] = frozenset()
                       ) -> dict[str, dict[str, Any]]:
    """should_pass protection: a company the sieve says should pass (source 'sieve', want explicit / partial) whose
    L2 label (on the mean) is below that level, or below its label in the base run (from_run), gets
    SHOULD_PASS_READS more reads (the next unused read indices, it alone in the packet order of the band reads) and
    is decided on the mean of all its reads. l2_res / l2_agg are updated in place. A company read from its profile
    alone (profile_keys; its label already capped at partial, 仅简介) is wanted at most at partial ('want_eff'): it
    is never re-read for a level it cannot reach, and it is always reported ('capped'), re-read or not.
    Returns {company_key: {want, want_eff, capped, before, after, extra_reads, planned, held}} of every company
    re-read or capped (never silent: the row and the check carry it)."""
    want_of = _should_pass_wants(sv, by_key)
    eff = {k: ("partial" if k in profile_keys else want) for k, want in want_of.items()}
    capped = {k for k, want in want_of.items() if eff[k] != want}

    def floor(k: str) -> int:                 # the level a company must reach: its wanted one, its base-run label
        base = base_labels.get(k)
        if base == "explicit" and k in profile_keys:
            base = "partial"
        return max(_LEVEL[eff[k]], _LEVEL.get(base, 0))
    todo: dict[str, tuple[int, ...]] = {}
    for k, want in want_of.items():
        r = l2_res.get(k) or {}
        if r.get("status") != "ok" or k not in seqs:
            continue
        lvl = _LEVEL.get(r.get("label"), 0)
        if lvl < floor(k):
            used = {i for i, _ in seqs[k]}
            idx, i = [], 1
            while len(idx) < SHOULD_PASS_READS:
                if i not in used:
                    idx.append(i)
                i += 1
            todo[k] = tuple(idx)
    out: dict[str, dict[str, Any]] = {
        k: {"want": want_of[k], "want_eff": eff[k], "capped": True, "before": l2_res[k].get("label"),
            "after": l2_res[k].get("label"), "extra_reads": 0, "planned": 0, "held": True}
        for k in capped if k not in todo and (l2_res.get(k) or {}).get("status") == "ok"}
    if not todo:
        return out
    items = {it.item_id: it for it in l2_items}
    before = {k: l2_res[k].get("label") for k in todo}
    for r in sorted({i for idx in todo.values() for i in idx}):
        its = sorted((items[k] for k, idx in todo.items() if r in idx and k in items),
                     key=lambda it: hashlib.sha256(f"{r}:{it.item_id}".encode("utf-8")).hexdigest())
        if not its:
            continue
        got, err_status, _err = _call_classify(client, its, dataclasses.replace(q2, read=r))
        for it, x in zip(its, got):
            seqs[it.item_id].append((r, x))
        if err_status:
            break
    for k in todo:
        seq = seqs[k]
        agg = aggregate_reads(seq)
        n_extra = sum(1 for i, x in seq if i in todo[k] and x.get("status") == "ok")
        if agg is not None and n_extra:
            agg["edge"] = L2_EDGE[0] <= agg["p_pos"] < L2_EDGE[1]
            agg["note"] = None
            l2_agg[k] = agg
            l2_res[k] = {**l2_res[k], "label": agg["label"], "probs": agg["probs"]}
        after = l2_res[k].get("label")
        if after == "explicit" and k in profile_keys:
            after = "partial"                 # what the row will show (the caller caps l2_res)
        held = _LEVEL.get(after, 0) >= floor(k)
        out[k] = {"want": want_of[k], "want_eff": eff[k], "capped": k in capped, "before": before[k],
                  "after": after, "extra_reads": n_extra, "planned": len(todo[k]), "held": held}
    return out


def _calibration_summary(sv: dict[str, Any], sieve_path: Path | None, params: dict[str, Any],
                         by_key: dict[str, dict], forced_keys: list[str], l1_res: dict[str, dict],
                         l2_res: dict[str, dict], passes: list[dict], out_rows: list[dict],
                         excluded_rows: list[dict], weak_by_lang: dict[str, list[str]], max_out: int,
                         should_pass: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """result['calibration']: what the sieve changed in this run (report.py renders the 校准 section from it)."""
    examples = [ex for ex in sv.get("examples") or [] if isinstance(ex, dict)]
    answers = [ex for ex in examples if ex.get("source", "card") == "card"]
    rules = sorted({(r if isinstance(r, str) else r.get("id")) for r in sv.get("rules") or []
                    if isinstance(r, (str, dict))} - {None})
    by_sid = {c["security_id"]: c for c in by_key.values()}
    pass_keys = {c["company_key"] for c in passes}
    rank_of = {r["company_key"]: r["rank"] for r in out_rows}
    checks = []
    for ex in examples:
        if ex.get("source") != "sieve":
            continue
        c = by_key.get(ex.get("company_key")) or by_sid.get(ex.get("security_id"))
        k = (c or {}).get("company_key")
        r2 = l2_res.get(k) or {}
        chk = {"security_id": ex.get("security_id") or (c or {}).get("security_id"), "company_key": k,
               "name": (c or {}).get("name") or ex.get("name"), "want": ex.get("want"),
               "in_universe": c is not None, "l1_pass": k in pass_keys,
               "l2_label": r2.get("label") if r2.get("status") == "ok" else None,
               "rank": rank_of.get(k), "in_output": k in rank_of}
        sp = (should_pass or {}).get(k)
        if sp is not None:
            chk.update(extra_reads=sp["extra_reads"], flag_zh=should_pass_flag_zh(sp))
        checks.append(chk)
    forced = [{"security_id": by_key[k]["security_id"], "company_key": k, "name": by_key[k]["name"],
               "l1_label": (l1_res.get(k) or {}).get("label"), "l1_pass": k in pass_keys,
               "l2_label": (l2_res.get(k) or {}).get("label")} for k in forced_keys]
    kw = sv.get("keywords") if isinstance(sv.get("keywords"), dict) else {}
    return {"sieve_path": params.get("sieve_path"), "sieve_version": sv.get("version"),
            "sieve_sha256": params.get("sieve_sha256"), "idea_key": sv.get("idea_key"),
            "answers": len(answers), "pins": sum(1 for ex in answers if ex.get("pin")), "rules": rules,
            "facets": bool(sv.get("facets")),
            "keywords_add": {lang: list(v.get("add") or []) for lang, v in kw.items() if isinstance(v, dict)
                             and v.get("add")},
            "keywords_weak": weak_by_lang,
            "keyword_log": {lang: list(v.get("log") or []) for lang, v in kw.items() if isinstance(v, dict)
                            and v.get("log")},
            "rejected_rules": [{"id": r.get("id"), "why_zh": r.get("why_zh")} for r in sv.get("rejected_rules") or []
                               if isinstance(r, dict)],
            "forced": forced, "checks": checks, "excluded": len(excluded_rows),
            "user_rows": sum(1 for r in out_rows if r.get("verdict_source") == "user"),
            "backfill": sum(1 for r in out_rows if r.get("backfill")),
            "summary_zh": f"已加载校准：{len(answers)} 条回答，{len(rules)} 条规则"}


def _csv_cell(v: Any) -> Any:
    if v is None:
        return ""
    return ";".join(map(str, v)) if isinstance(v, (list, tuple)) else v


SHELLS_CSV_COLUMNS = ("security_id", "company_key", "name", "market_cap_usd", "rule", "pattern", "industry",
                      "revenue_ttm_usd")


def _csv_text(columns: Iterable[str], rows: Iterable[dict[str, Any]]) -> str:
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=list(columns), extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: _csv_cell(r.get(k)) for k in columns})
    return buf.getvalue()


def write_outputs(result: dict[str, Any], out_path: Path, l2_inputs: list[dict[str, Any]] | None = None,
                  ledger: tuple[dict[str, Any], list[dict[str, Any]]] | None = None, *, atomic: bool = False) -> None:
    """results.csv, results.json, report.md and, when given, l2_inputs.jsonl (one line per L2 input: the text
    layer 2 read, its excerpts, keyword hit, matched terms and evidence_sha; used by the calibration cards),
    funnel.jsonl.gz (the run ledger, always written through a temp file + os.replace) and, when the shells filter
    dropped companies, shells_dropped.csv.

    atomic=True (the on-demand update pass): every file is written to a temp name first and then os.replace'd; an
    existing results.json is kept as results.v<N>.json (the first free N) before the new one replaces it."""
    import gzip
    from . import report
    out_path.mkdir(parents=True, exist_ok=True)
    files = {"results.csv": _csv_text(CSV_COLUMNS, result["rows"]),
             "results.json": json.dumps(result, ensure_ascii=False, indent=2, default=str)}
    dropped = (result.get("gaps") or {}).get("shells_dropped")
    if dropped:
        files["shells_dropped.csv"] = _csv_text(SHELLS_CSV_COLUMNS, dropped)
    if ledger is not None:
        header, lines = ledger
        body = "\n".join(json.dumps(x, ensure_ascii=False, separators=(",", ":"), default=str)
                         for x in [header, *lines]) + "\n"
        tmp = out_path / f".{LEDGER_NAME}.tmp{os.getpid()}"
        tmp.write_bytes(gzip.compress(body.encode("utf-8"), compresslevel=6, mtime=0))
        os.replace(tmp, out_path / LEDGER_NAME)
    if l2_inputs is not None:
        files["l2_inputs.jsonl"] = "".join(json.dumps(line, ensure_ascii=False, sort_keys=True, default=str) + "\n"
                                           for line in l2_inputs)
    files["report.md"] = report.render_markdown(result)
    if not atomic:
        for name, text in files.items():
            with open(out_path / name, "w", newline="" if name.endswith(".csv") else None, encoding="utf-8") as f:
                f.write(text)
        return
    tmps = {}
    for name, text in files.items():
        tmp = out_path / f".{name}.{os.getpid()}.tmp"
        with open(tmp, "w", newline="" if name.endswith(".csv") else None, encoding="utf-8") as f:
            f.write(text)
        tmps[name] = tmp
    # results.json never goes missing: the old one is linked (or copied) to results.v<N>.json and replaced last, so
    # an interrupt or an OSError on the way leaves the previous results.json (and no stray temp files)
    old = out_path / "results.json"
    kept: Path | None = None
    done = False
    try:
        if old.exists():
            n = 1
            while (out_path / f"results.v{n}.json").exists():
                n += 1
            kept = out_path / f"results.v{n}.json"
            try:
                os.link(old, kept)
            except OSError:
                shutil.copy2(old, kept)
        for name in sorted(tmps, key=lambda x: x == "results.json"):
            os.replace(tmps[name], out_path / name)
        done = True
    finally:
        if not done:
            for tmp in tmps.values():
                with contextlib.suppress(OSError):
                    tmp.unlink()
            if kept is not None:
                with contextlib.suppress(OSError):
                    kept.unlink()
