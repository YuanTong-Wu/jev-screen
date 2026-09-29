"""Your AI's review (the scope-questions design §6): the user's own AI reads the same excerpt the system read
and makes the company-level calls; only the few it is unsure about, or where it strongly disagrees with the system,
go to the human.

- decks (jevscreen.agent_deck/1): part A (blocks the relay: every listed row - the whole to-confirm section and
  the whole main list; a single padded list: the top 10 - the side-V rows of the provisional scope splits, at most 3
  below-cut / gap rows), part B (never blocks: the listed rows part A did not hold, strong rows removed by scope,
  below-cut, gap) and follow-up parts F<n>; each item carries the exact L2 text Jev read, sentence-numbered, and,
  when the company has a stored official filing, up to MORE_MAX more numbered sentences of it (evidence.more: the
  product / business passages the excerpt left out; add_more);
- answers (jevscreen.agent_answers/1): yes / no / unsure per item, citing sentence numbers (quote_ids), never
  copied quotes;
- the agent layer (<home>/sieves/<idea_key>.agent.json, jevscreen.agent_verdicts/1): a lower-precedence layer of
  its own. It never sets user_verdict, never re-scores, never lists an unverified company; a verdict applies only to
  the evidence (evidence_sha) it was given for.

Pure functions plus the agent file's IO (a file lock and a version check like save_sieve). No network.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

AGENT_FORMAT = "jevscreen.agent_verdicts/1"
DECK_FORMAT = "jevscreen.agent_deck/1"
ANSWERS_FORMAT = "jevscreen.agent_answers/1"
REVIEW_FORMAT = "jevscreen.review/1"
# part A reads every listed row (max_out <= 40: to-confirm + main), the held rows and 3 edge rows: <= 60
DECK_A_MAX, DECK_A_TOP, DECK_A_HELD, DECK_A_EDGE = 60, 10, 12, 3
DECK_B_MAX, BELOW_CUT_MIN, BELOW_CUT_MAX, REMOVED_MAX, GAP_MAX = 45, 5, 12, 5, 3
FOLLOWUP_MAX = 20
MORE_MAX = 5                   # stored-filing sentences added to an item (evidence.more)
NO_NEW = 1.0                   # a more-sentence that adds no new idea term counts this much less
SHORT_PIECE = 16               # a filing piece shorter than this (flat characters) is joined to the next
SENT_MAX = 240                 # a sentence longer than this is split at the nearest boundary
WHY_MAX = {"zh": 80, "en": 160}
SHORT_MAX = {"zh": 10, "en": 40}
QUOTE_TR_MAX = 200
QUOTE_MAX = 200
QUOTE_IDS_MAX = 3
E2_P_EXPLICIT = 0.80
AGENT_REVIEW_TIMEOUT_S = 1200     # part A reads every listed row: typically 5-10 minutes
STATES = ("applied", "escalated", "held", "not_applied")
LICENCE_NOTE = ("Personal use. Excerpts from official filings (official-private) and company profiles "
                "(gray-private); keep this file on this computer.")
YES_LEVELS = ("explicit", "partial")
UNSURE_KINDS = ("thin", "meaning")
CHIPS = ("c", "d", "e", "f", "g", "h", "i", "j", "k")


class AgentError(ValueError):
    """A bad answers file / deck / token (exit 1). text_zh / text_en carry the plain message."""

    def __init__(self, zh: str, en: str):
        super().__init__(en)
        self.text_zh, self.text_en = zh, en


class AgentStale(ValueError):
    """The agent file changed on disk since it was read: re-read and apply again."""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------------------------------------ sentences

_TAG = re.compile(r"^\[[^\n]*\]\s*$")
_BLOCKS = re.compile(r"\n\s*\[\.\.\.\]\s*\n|\n+")
_SOFT = re.compile(r"[，,；;、:：]\s*|\s+")


def body_of(text: str | None) -> str:
    """The L2 text without its first '[...]' tag line (what the evidence says, not how it was built)."""
    t = text or ""
    first, sep, rest = t.partition("\n")
    return rest.lstrip("\n") if sep and _TAG.match(first.strip()) else t


def _cut(s: str) -> list[str]:
    out = []
    while len(s) > SENT_MAX:
        cut = None
        for m in _SOFT.finditer(s, 0, SENT_MAX):
            if m.end() >= SENT_MAX // 2:
                cut = m.end()
        cut = cut or SENT_MAX
        out.append(s[:cut].strip())
        s = s[cut:].strip()
    if s:
        out.append(s)
    return [x for x in out if x]


def sentences(text: str | None) -> list[list[Any]]:
    """[[1, 'first sentence'], [2, ...]] of an L2 text (tag line removed): a pure, deterministic split at paragraph
    breaks, '[...]' excerpt separators and sentence ends (screen._sent_ends: CJK and ASCII full stops, enumerators
    excluded); a piece longer than SENT_MAX is split at the nearest soft boundary. The same function resolves the
    quote_ids of an answer at render time."""
    from . import screen
    pieces: list[str] = []
    for block in _BLOCKS.split(body_of(text)):
        block = block.strip()
        if not block:
            continue
        start = 0
        for e in screen._sent_ends(block) + [len(block)]:
            s = block[start:e].strip()
            if s:
                pieces += _cut(s)
            start = e
    return [[i, s] for i, s in enumerate(pieces, 1)]


def quote_of(text: str | None, ids: Iterable[int], limit: int = QUOTE_MAX) -> str:
    """The cited sentences of a text joined, clipped to `limit` characters."""
    sents = dict((i, s) for i, s in sentences(text))
    q = " ".join(sents[i] for i in ids if i in sents)
    return q if len(q) <= limit else q[: limit - 1].rstrip() + "…"


def cited_text(text: str | None, ids: Iterable[int] | None, quotes: dict[str, str] | None = None,
               limit: int = QUOTE_MAX) -> str:
    """quote_of with the verdict's own record of what it cited (`quotes`, {"<id>": sentence}: the text your AI was
    shown, including evidence.more sentences, which the L2 text does not hold) taking precedence."""
    q = quotes if isinstance(quotes, dict) else {}
    ids = list(ids or [])
    sents = dict((i, s) for i, s in sentences(text)) if any(str(i) not in q for i in ids) else {}
    out = " ".join(p for p in (q.get(str(i)) or sents.get(i) for i in ids) if p)
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


# ------------------------------------------------------------------------------------------------ more sentences

# the company speaking of itself / of what it sells, and industry or forecast talk (a sentence about the market, not
# the company's own offer, ranks lower)
_OWN = re.compile(r"本公司|公司|我们|我司|本集团|集团|当社|当グループ|당사|회사|\b(?:we|our|the company|the group)\b",
                  re.I)
_OFFER = re.compile(r"产品|製品|제품|业务|事業|사업|批量|量产|出货|供货|订单|客户|受注|出荷|양산|수주|납품|"
                    r"\b(?:products?|solutions?|segments?|customers?|ship(?:s|ped|ping|ments?)?|orders?|sells?|"
                    r"offers?|provides?)\b", re.I)
_TREND = re.compile(r"行业|市场规模|据[^。]{0,20}(?:统计|预测|数据)|预计|渗透率|市場規模|業界|시장\s*규모|업계|"
                    r"\b(?:industry|market size|forecast|according to|is expected to|are expected to)\b", re.I)
# single English words of an idea sentence that name no product (the multi-word phrases stay)
_EN_PLAIN = frozenset("""listed public suppliers supplier make makes making sell sells selling own owning providing
    provide provides manufacturers manufacturer makers maker designers designer small companies company used using
    serving operating developing building designing contract vendors vendor producers producer leading core key main
    major primary global based services service systems system products product solutions solution equipment
    makers""".split())


def _flat(t: str | None) -> str:
    return re.sub(r"\s+", "", t or "").replace("…", "")


_END = re.compile(r"[。！？!?；;.]\s*$")


def _pieces(text: str) -> list[str]:
    """The sentences of a filing text for more_sentences: sentences(), a piece shorter than SHORT_PIECE characters
    that does not end a sentence (a table cell or a line the PDF extraction broke: '人形机器人核心' | '驱动模组') joined
    to the next one, and exact repeats (a summary and the full report say the same) dropped."""
    out: list[str] = []
    buf = ""
    for _i, x in sentences(text):
        buf = f"{buf} {x}".strip() if buf else x
        if len(_flat(buf)) < SHORT_PIECE and not _END.search(buf):
            continue
        out.append(buf)
        buf = ""
    if buf:
        if out and len(out[-1]) + len(buf) + 1 <= SENT_MAX:
            out[-1] = f"{out[-1]} {buf}"
        else:
            out.append(buf)
    seen: set[str] = set()
    uniq = []
    for x in out:
        f = _flat(x)
        if f not in seen:
            seen.add(f)
            uniq.append(x)
    return uniq


def more_sentences(text: str | None, *, shown: str | None, strong: Iterable[str], weak: Iterable[str] = (),
                   anchors: Iterable[str] = (), start: int = 1, max_n: int = MORE_MAX) -> list[list[Any]]:
    """[[start, sentence], ...]: up to max_n sentences of a stored filing's text (_pieces) that the excerpt (`shown`)
    did not already hold in full and that speak of the idea. Score: 2 for the first Latin anchor hit (HBM, SiC, 800G)
    + 0.5 per further one, + 1 per distinct strong term, + 0.5 per distinct weak term (at least one anchor or strong
    term, or three weak ones), + 0.5 when the company speaks of itself, + 0.5 when it names an offer (products,
    shipments, customers, orders), - 2 for industry / forecast talk. Picked greedily, best first (ties: earlier); a
    sentence that adds no term the picked ones lack counts NO_NEW less (the product list, not five copies of one
    claim); kept at >= 1.5 and returned in document order, numbered from `start`. A kept sentence ending in a colon
    brings the next one (the table it introduces) while there is room. Pure and deterministic."""
    from . import screen
    if not text:
        return []
    seen: set[str] = set()
    pats: list[tuple[re.Pattern, str, str]] = []
    for group, kind in ((anchors, "a"), (strong, "s"), (weak, "w")):
        for t in group or ():
            k = str(t or "").strip().casefold()
            if k and k not in seen:
                seen.add(k)
                pats.append((screen._term_pattern(str(t)), kind, k))
    if not pats:
        return []
    flat_shown = _flat(shown)
    sents = _pieces(text)
    cands: dict[int, tuple[float, set[str]]] = {}
    for j, x in enumerate(sents):
        f = _flat(x)
        if len(f) < 6 or (flat_shown and f in flat_shown):
            continue
        hit = [(kind, k) for p, kind, k in pats if p.search(x)]
        n_a = sum(1 for kind, _k in hit if kind == "a")
        n_s = sum(1 for kind, _k in hit if kind == "s")
        n_w = sum(1 for kind, _k in hit if kind == "w")
        if not (n_a or n_s or n_w >= 3):
            continue
        score = (2.0 + 0.5 * (n_a - 1) if n_a else 0.0) + n_s + 0.5 * n_w \
            + 0.5 * bool(_OWN.search(x)) + 0.5 * bool(_OFFER.search(x)) - 2.0 * bool(_TREND.search(x))
        if score >= 1.5:
            cands[j] = (score, {k for _kind, k in hit})
    pick: list[int] = []
    covered: set[str] = set()
    while cands and len(pick) < max_n:
        best = max(cands, key=lambda j: (cands[j][0] - (NO_NEW if cands[j][1] <= covered else 0.0), -j))
        sc, ks = cands.pop(best)
        if sc - (NO_NEW if ks <= covered else 0.0) < 1.5:
            break
        pick.append(best)
        covered |= ks
    pick.sort()
    for j in list(pick):
        if len(pick) >= max_n:
            break
        if sents[j].rstrip().endswith((":", "：")) and j + 1 < len(sents) and j + 1 not in pick \
                and _flat(sents[j + 1]) not in flat_shown:
            pick = sorted(pick + [j + 1])
    return [[start + k, sents[j]] for k, j in enumerate(pick)]


def _more_terms(result: dict[str, Any], lang: str) -> tuple[list[str], list[str], list[str]]:
    """(strong, weak, anchors) the more-sentence search uses for a filing in `lang`: the run's excerpt terms of that
    language (generic ones dropped; single plain English words weak), the idea's own words and 2-character pieces
    (weak), and the Latin anchors of the idea (HBM, SiC, 800G: written the same way in CJK filings)."""
    from . import retrieval, screen
    tb = result.get("terms_by_lang") or {"en": result.get("terms") or []}
    cal = result.get("calibration") if isinstance(result.get("calibration"), dict) else {}
    weak_cal = {str(t).casefold() for v in (cal.get("keywords_weak") or {}).values() for t in v or []}

    def plain(t: str) -> bool:
        return " " not in t.strip() and t.strip().casefold() in _EN_PLAIN

    base = [t for t in screen.terms_for(tb, lang) if t and not retrieval.is_generic(t, lang)
            and str(t).casefold() not in weak_cal]
    strong = [t for t in base if not plain(t) and not (" " not in t.strip() and t.isascii() and len(t) <= 3)]
    weak = [t for t in base if t not in strong and not plain(t)]
    weak += [p for p in screen.excerpt_terms(strong, lang) if p not in strong]
    weak += [t for t in _terms(result) if t not in strong and not plain(t) and not retrieval.is_generic(t, lang)
             and str(t).casefold() not in weak_cal]
    en = screen.terms_for(tb, "en")
    anchors = retrieval.latin_anchors(" ".join([str(result.get("idea") or ""), str(result.get("idea_en") or "")]
                                               + [str(t) for t in en]))
    return strong, retrieval._dedup(weak), anchors


def review_sha(evidence_sha: str | None, more: list[list[Any]] | None) -> str:
    raw = json.dumps([evidence_sha, more or []], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def add_more(deck: dict[str, Any], docs: dict[str, dict[str, Any]] | None, result: dict[str, Any], *,
             max_n: int = MORE_MAX) -> dict[str, Any]:
    """The deck with evidence.more on every item whose company has a stored official filing (`docs`: company_key ->
    {text, source_id, form, filing_date}; review_cli.stored_filings): up to max_n more numbered sentences
    (more_sentences, numbered after the excerpt's), and review_sha over the excerpt's evidence_sha and them. A
    profile-only item gets the filing's sentences too (the filing is official text the profile is not). Items
    without a stored filing are left as they are. Pure."""
    from . import screen
    cache: dict[str, tuple[list[str], list[str], list[str]]] = {}
    for it in deck.get("items") or []:
        d = (docs or {}).get(it.get("company_key") or "") or {}
        text = d.get("text")
        ev = it.get("evidence") or {}
        if not text:
            continue
        src = d.get("source_id")
        lang = screen.detect_language(text, default=screen.OFFICIAL_DOC_SOURCES.get(src or "", ("", "en"))[1])
        if lang not in cache:
            cache[lang] = _more_terms(result, lang or "en")
        strong, weak, anchors = cache[lang]
        sents = ev.get("sentences") or []
        shown = " ".join(x for _i, x in sents)
        more = more_sentences(text, shown=shown, strong=strong, weak=weak, anchors=anchors,
                              start=len(sents) + 1, max_n=max_n)
        if not more:
            continue
        ev["more"] = {"sentences": more, "source": screen.source_label(src), "form": d.get("form"),
                      "filing_date": str(d["filing_date"])[:10] if d.get("filing_date") else None, "lang": lang}
        it["evidence"] = ev
        it["review_sha"] = review_sha(it.get("evidence_sha"), more)
    return deck


# ------------------------------------------------------------------------------------------------ agent file

def agent_path(cfg, idea: str) -> Path:
    from . import calib
    p = calib.sieve_path(cfg, idea)
    return p.with_name(p.stem + ".agent.json")


def new_agent(idea: str) -> dict[str, Any]:
    from . import keywords
    return {"format": AGENT_FORMAT, "idea_key": keywords.idea_key(idea), "idea": idea.strip(), "version": 0,
            "verdicts": [], "history": []}


def load_agent(cfg, idea: str) -> dict[str, Any]:
    """The idea's agent file, or a new empty one (version 0). A broken file reads as empty (it never stops a run)."""
    p = agent_path(cfg, idea)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return new_agent(idea)
    if not isinstance(data, dict) or data.get("format") != AGENT_FORMAT:
        return new_agent(idea)
    data.setdefault("verdicts", [])
    data.setdefault("history", [])
    data.setdefault("version", 0)
    return data


def save_agent(cfg, idea: str, doc: dict[str, Any]) -> dict[str, Any]:
    """Write the agent file atomically under a file lock; AgentStale when another writer saved in between."""
    from . import calib
    p = agent_path(cfg, idea)
    base = int(doc.get("version") or 0)
    out = {**doc, "format": AGENT_FORMAT, "version": base + 1, "updated_at": now_iso()}
    with calib._file_lock(p):
        disk = load_agent(cfg, idea) if p.exists() else None
        if disk is not None and int(disk.get("version") or 0) != base:
            raise AgentStale(f"{p} changed on disk (version {disk.get('version')}, read {base})")
        tmp = p.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)
    return out


def current(doc: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    return {v["company_key"]: v for v in (doc or {}).get("verdicts") or [] if isinstance(v, dict)
            and v.get("company_key")}


def agent_verdicts(cfg, idea: str) -> dict[str, dict[str, Any]]:
    """{company_key: verdict} of the idea's current agent verdicts ({} without a file)."""
    return current(load_agent(cfg, idea))


def put_verdicts(doc: dict[str, Any], verdicts: list[dict[str, Any]], *, now: str | None = None) -> dict[str, Any]:
    """doc' with `verdicts` in place: a newer verdict for the same company moves the older one to history."""
    now = now or now_iso()
    d = copy.deepcopy(doc)
    cur = current(d)
    for v in verdicts:
        old = cur.get(v["company_key"])
        if old is not None:
            d.setdefault("history", []).append({**old, "superseded_at": now})
        cur[v["company_key"]] = v
    d["verdicts"] = list(cur.values())
    return d


def version_of(cfg, idea: str) -> int:
    return int(load_agent(cfg, idea).get("version") or 0)


# ------------------------------------------------------------------------------------------------ the layer

def _same(a: Any, b: Any) -> bool:
    return bool(a) and bool(b) and a == b


def apply_agent(kept: list[dict[str, Any]], agent: dict[str, dict[str, Any]] | None, *,
                human_keys: set[str] | None = None, kept_chips: set[str] | None = None
                ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """(kept', excluded_by_agent, notes): screen.pin_and_rank's step 3. Only a verdict on the row's own evidence
    (same evidence_sha) counts, and a human pin always wins (the row is left to calib.apply_pins).
    - applied no -> excluded with verdict_source 'agent' (unless the human said 要 to that kind: not applied);
    - applied yes -> tagged agent_verdict 'yes' (verdict_source becomes evidence+agent); no re-score, no listing;
    - applied unsure -> tagged; thin evidence (or an excerpt that does not mention the idea) gets the agent_thin gap;
    - held / escalated / not_applied -> tagged only."""
    human_keys = human_keys or set()
    kept_chips = kept_chips or set()
    out, excluded, notes = [], [], []
    skipped_kind = 0
    for e0 in kept:
        e = dict(e0)
        ck = e.get("company_key")
        v = (agent or {}).get(ck)
        if not v or ck in human_keys or e.get("security_id") in human_keys \
                or not _same(v.get("evidence_sha"), e.get("evidence_sha")):
            out.append(e)
            continue
        e.update(agent_verdict=v.get("v"), agent_state=v.get("state"), agent_chip=v.get("chip"),
                 agent_why_zh=v.get("why_zh"), agent_why_en=v.get("why_en"), agent_quote_ids=v.get("quote_ids"),
                 agent_level=v.get("level"), agent_quotes=v.get("quotes") or None)
        if v.get("state") != "applied":
            out.append(e)
            continue
        if v.get("v") == "no":
            if v.get("chip") in kept_chips:
                e["agent_state"] = "not_applied"
                e["agent_note"] = "kept_kind"
                skipped_kind += 1
                out.append(e)
                continue
            e["verdict_source"] = "agent"
            excluded.append(e)
            continue
        if v.get("v") == "unsure" and (v.get("unsure_kind") == "thin" or v.get("mentions_idea") is False):
            e["agent_thin"] = True
        out.append(e)
    if skipped_kind:
        notes.append(f"你说过这类要：你的 AI 判为这一类的 {skipped_kind} 家没有移出 / you said to keep this kind: "
                     f"{skipped_kind} your AI judged to be of it stay")
    return out, excluded, notes


# ------------------------------------------------------------------------------------------------ decks

def chip_words(sieve: dict[str, Any] | None) -> dict[str, dict[str, str]]:
    """{chip: {zh, en}} of the no-chips (calib.py's words; g with the facets' words)."""
    from . import calib
    zh = dict(calib.NO_CHIP_TEXT)
    en = dict(calib.NO_CHIP_TEXT_EN)
    fz = (sieve or {}).get("facets_zh") if isinstance((sieve or {}).get("facets_zh"), dict) else {}
    zh["g"] = (f"只有大类，没提「{fz['target']}」" if fz.get("target") else calib.G_CHIP_GENERIC_ZH)
    en["g"] = calib.g_chip_en(calib._facets(sieve))
    return {c: {"zh": zh.get(c, c), "en": en.get(c, c)} for c in CHIPS}


def criteria_en(result: dict[str, Any], sieve: dict[str, Any] | None) -> list[str]:
    """What the agent judges by: the L2 criteria (with the sieve's rules, as the run asked Jev), then the human's
    scope answers and the idea-wording defaults as plain sentences."""
    from . import scope
    q = ((result.get("questions") or {}).get("l2") or {})
    out = [f"{k}: {v}" for k, v in (q.get("criteria") or {}).items()]
    fsha = scope.facets_sha(sieve)
    for e in scope._entries(sieve):
        if e.get("facets_sha") != fsha or e.get("answer") not in ("yes", "no"):
            continue
        kind = scope.kind_words(f"{e['family']}.{e['value']}", sieve, "en")
        who = "The human said" if e.get("source") == "human" else f"By the idea's own words ({e.get('because')})"
        out.append(f"{who}: companies that {kind} do {'NOT ' if e['answer'] == 'no' else ''}count.")
    return out


INSTRUCTIONS_EN = (
    "You are the human's own AI. For every item decide from evidence.sentences and evidence.more ONLY whether the "
    "company offers what the idea describes (criteria_en, including the human's own scope answers). Do not use what "
    "you know about the company from elsewhere; the name alone is not evidence. evidence.more, when present, holds a "
    "few more numbered sentences of the same company's stored official filing (its source, form and filing_date "
    "given) that the excerpt left out, often the product list: cite them by number like the others; an "
    "industry-trend or plan sentence proves no more there than in the excerpt. Answer per item number: v 'yes' with "
    "level 'explicit' (it offers exactly this) or 'partial' (related, small or early); v 'no' with the fitting chip "
    "from chips; v 'unsure' with unsure_kind 'thin' (too little text) or 'meaning' (the text is there but unclear). "
    "Every yes/no cites 1-3 quote_ids (sentence numbers; an unsure may cite the sentences it is unsure about) and "
    "gives 'why' in the human's language only (human_lang; at most 80 Chinese characters or 160 English characters; "
    "a why or short in the other language is dropped). When evidence.lang differs from human_lang add quote_tr (a "
    "translation of the cited sentences, at most 200 characters). Held items (held_sid set) also need in_group "
    "(true when the company really is the kind named in held_questions) and short (what the company is, at most 10 "
    "Chinese characters or 40 English characters). Items with section 'to_confirm' are not on the human's list: a "
    "yes with level 'explicit' and its quote_ids moves one into the main list, marked as checked by you, so give it "
    "only when the cited sentences state it plainly (else 'partial', which leaves it to confirm). Items with section "
    "'main' are on the human's list: when the text contradicts a hard part of the idea (another geography, a "
    "different product kind, only a plan, a goal, 布局 or R&D, or the idea's product only named in a list of many) "
    "answer 'no' with the fitting chip, which takes it off the main list; 'partial' on a main item is for an offer "
    "that is real but small or early. Skip an item you cannot judge: a missing item is 'not reviewed'. Save the "
    "file as answer_schema shows and run record_command.")
ANSWER_SCHEMA = {"format": ANSWERS_FORMAT, "deck_id": "<deck_id>", "agent": "<optional: product/model>",
                 "answers": {"1": {"v": "yes", "level": "explicit|partial", "quote_ids": [3],
                                   "why": "<human_lang>", "quote_tr": "<only when evidence.lang differs>"},
                             "2": {"v": "no", "chip": "k", "quote_ids": [2], "why": "..."},
                             "3": {"v": "unsure", "unsure_kind": "thin|meaning", "quote_ids": [4], "why": "..."},
                             "7": {"v": "no", "chip": "k", "quote_ids": [1], "why": "...", "in_group": True,
                                   "short": "..."}}}


def _terms(result: dict[str, Any]) -> list[str]:
    from . import page
    return page.idea_terms(result)


def _item(n: int, group: str, row: dict[str, Any], inp: dict[str, Any], facets_map: dict[str, Any] | None,
          names_zh: dict[str, str], terms: list[str], held: tuple[str, str] | None = None) -> dict[str, Any]:
    from . import calib, page
    text = inp.get("text") or ""
    source, form, fdate = calib._filing_of(inp, row)
    hits = page.mentions_idea(body_of(text), terms)
    fac = {f: {"label": x.get("label"), "p": x.get("p")} for f, x in ((facets_map or {}).get(row["company_key"])
                                                                        or {}).items() if isinstance(x, dict)}
    return {"n": n, "group": group, "held_sid": held[0] if held else None, "held_kind": held[1] if held else None,
            "company_key": row["company_key"], "security_id": row.get("security_id"), "name": row.get("name"),
            "name_zh": names_zh.get(row.get("security_id") or ""), "country": row.get("country"),
            "rank": row.get("rank"),
            # the page section the row is in now: main (the confirmed list) or to_confirm (null: not listed / a
            # single padded list)
            "section": ({"high": "main", "confirm": "to_confirm"}.get(row.get("shortlist_tier") or "")
                        if row.get("rank") is not None else None),
            "system": {"label": row.get("l2_label"), "p_pos": row.get("l2_p_pos"),
                       "p_explicit": row.get("l2_p_explicit"), "edge": row.get("l2_edge"), "facets": fac,
                       # screen --judge: the tier and, for C, the counter-evidence read (an inference to check)
                       **({"judge": {k: row.get(f"judge_{k}") for k in ("tier", "labels", "reason")}}
                          if row.get("judge_tier") else {})},
            "evidence": {"kind": inp.get("evidence") or row.get("l2_evidence"),
                         "source": None if source == calib.PROFILE_FILING.rstrip("|") else source, "form": form,
                         "filing_date": fdate, "lang": inp.get("lang"), "sentences": sentences(text)},
            "evidence_sha": inp.get("evidence_sha") or row.get("evidence_sha"),
            "mentions_idea": None if hits is None else bool(hits)}


def _decided(sieve: dict[str, Any] | None) -> set[str]:
    """company_key / security_id of every company a human already decided (pins, should_pass / should_fail)."""
    from . import calib
    out: set[str] = set()
    for ex in (sieve or {}).get("examples") or []:
        if not isinstance(ex, dict):
            continue
        if ex.get("source") == "sieve" or (ex.get("source", "card") == "card" and ex.get("pin")):
            out |= {ex.get("company_key"), ex.get("security_id")} - {None}
    out |= set(calib.pins(sieve))
    return out


def full_ranking(result: dict[str, Any], sieve: dict[str, Any] | None, pool: Iterable[dict[str, Any]] | None,
                 inputs: dict[str, Any] | None, agent: dict[str, dict[str, Any]] | None
                 ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """(every verified company in rank order, the unverified, the scope/agent exclusions) of a run with the sieve,
    the facets and the agent layer applied: screen.pin_and_rank on all candidates."""
    from . import calib, screen
    params = result.get("params") or {}
    max_out = int(params.get("max_out") or screen.SCREEN_DEFAULTS["max_out"])
    cands = calib._merge_candidates(result, pool)
    for c in cands:
        if not c.get("evidence_sha"):
            c["evidence_sha"] = ((inputs or {}).get(c["company_key"]) or {}).get("evidence_sha")
    verified, unverified = calib.ranking_entries(cands, sieve, params.get("rank") or "label")
    extra: dict[str, Any] = {}
    v2, u2, _ex, _n = screen.pin_and_rank(verified, unverified, sieve, max_out, facets=result.get("facets"),
                                          agent=agent, fsha=params.get("facets_sha"), out=extra)
    for i, e in enumerate(v2, 1):
        e["_pos"] = i
    return v2, u2, extra


def review_order(rows: list[dict[str, Any]], n: int = DECK_A_TOP) -> list[dict[str, Any]]:
    """The listed rows your AI reads (deck part A's 'top' group: n = DECK_A_TOP), in the order it reads them: with
    the sections, the first n rows of the to-confirm section (a yes there, level explicit, moves a row into the main
    list), then the first n of the main list (a no there moves it out); without them the first n ranks."""
    from . import shortlist
    ranked = sorted((r for r in rows if r.get("rank") is not None), key=lambda r: r["rank"])
    if not any(r.get("shortlist_tier") for r in ranked):
        return ranked[:n]
    main, confirm = shortlist.split(ranked)
    return confirm[:n] + main[:n]


def build_deck(result: dict[str, Any], inputs: dict[str, Any], sieve: dict[str, Any] | None,
               agent: dict[str, dict[str, Any]] | None, *, part: str, held: list[dict[str, Any]] | None = None,
               pool: Iterable[dict[str, Any]] | None = None, names_zh: dict[str, str] | None = None,
               human_lang: str = "zh", removed_so_far: int = 0, followup_keys: Iterable[str] | None = None,
               exclude_keys: Iterable[str] = ()) -> dict[str, Any]:
    """An agent deck (deterministic, no RNG). Left out: companies a human already decided and companies with an
    agent verdict on the same evidence. part 'A' (blocking, <= DECK_A_MAX): held = the side-V rows of `held`
    (provisional questions: [{sid, kind, family, value, v: [company_key]}], <= 12, highest facet p first), top =
    review_order (the whole to-confirm section, then the whole main list, up to max_out; ranks 1..10 for a single
    list), then below-cut + gap <= 3. part 'B' (not blocking, <= DECK_B_MAX): the listed rows not answered yet (same
    order), strong rows
    removed by scope (<= 5), below-cut (max(5, removals + 3), <= 12), gap (<= 3). part 'F<n>': `followup_keys`."""
    from . import calib, scope, screen
    params = result.get("params") or {}
    max_out = int(params.get("max_out") or screen.SCREEN_DEFAULTS["max_out"])
    names_zh = names_zh or {}
    pool = list(pool or [])
    decided = _decided(sieve)
    agent = agent or {}
    facets_map = result.get("facets") or {}
    terms = _terms(result)
    by_key = {c["company_key"]: c for c in calib._merge_candidates(result, pool)}
    skip = set(exclude_keys)

    def sha(k: str) -> str | None:
        return (inputs.get(k) or {}).get("evidence_sha") or (by_key.get(k) or {}).get("evidence_sha")

    def free(k: str) -> bool:
        c = by_key.get(k) or {}
        if k in skip or k in decided or c.get("security_id") in decided or not inputs.get(k):
            return False
        v = agent.get(k)
        return not (v and v.get("evidence_sha") and v["evidence_sha"] == sha(k))

    items: list[dict[str, Any]] = []
    used: set[str] = set()

    def add(group: str, k: str, held_: tuple[str, str] | None = None) -> bool:
        if k in used or not free(k):
            return False
        row = dict(by_key.get(k) or {"company_key": k})
        items.append(_item(len(items) + 1, group, row, inputs.get(k) or {}, facets_map, names_zh, terms, held_))
        used.add(k)
        return True

    rows = [r for r in result.get("rows") or [] if r.get("rank") is not None]
    rows.sort(key=lambda r: r["rank"])
    # the reads go to the to-confirm section first (its rows can move into the main list), then the main list;
    # a single padded list (no tiers) is read in rank order as before
    first = review_order(rows)
    v_all, _u, extra = full_ranking(result, sieve, pool, inputs, agent)
    below = [e["company_key"] for e in v_all if e["_pos"] > max_out]
    gap = [c["company_key"] for c in sorted(
        (c for c in by_key.values() if not c.get("_in_out") and c.get("l2_label") == "insufficient"
         and (((inputs.get(c["company_key"]) or {}).get("evidence")) or c.get("l2_evidence")) == "annual_report"
         and (c.get("l1_p_core") or 0.0) + (c.get("l1_p_adjacent") or 0.0) >= calib.GAP_L1_MIN),
        key=lambda c: (not (inputs.get(c["company_key"]) or {}).get("keyword_hit"), -(c.get("market_cap_usd") or 0),
                       c.get("security_id") or ""))]
    held_questions = []
    if part == "A":
        held_of: dict[str, tuple[str, str]] = {}
        for q in held or []:
            held_questions.append({"sid": q["sid"], "family": q["family"], "value": q["value"], "kind": q["kind"],
                                   "criterion_en": scope.criteria(q["family"], sieve).get(q["value"])})
            for k in q.get("v") or []:
                held_of.setdefault(k, (q["sid"], q["kind"]))
        ranked_held = sorted(held_of, key=lambda k: (
            -float((((facets_map.get(k) or {}).get(held_of[k][1].split(".")[0])) or {}).get("p") or 0.0),
            (by_key.get(k) or {}).get("rank") or 10 ** 6, k))
        n_held = 0
        for k in ranked_held:
            if n_held >= DECK_A_HELD:
                break
            n_held += add("held", k, held_of[k])
        from . import shortlist
        if any(r.get("shortlist_tier") for r in rows):
            # every listed row is read before the relay (the whole to-confirm section: a yes there moves a row into
            # the main list; the whole main list: a no takes a row out). Room for the main list is kept (its rows
            # are numbered on the page): the to-confirm rows fill what is left, and are still read first
            first = review_order(rows, n=max_out)
            main_first = shortlist.split(rows)[0][:max_out]
            n_main = sum(1 for r in main_first if r["company_key"] not in used and free(r["company_key"]))
            room = max(0, DECK_A_MAX - len(items) - n_main)
            main_keys = {r["company_key"] for r in main_first}
            for r in first:
                if r["company_key"] in main_keys:
                    add("top", r["company_key"])
                elif room > 0:
                    room -= add("top", r["company_key"])
        else:
            for r in first:
                add("top", r["company_key"])
        n_edge = 0
        for group, keys in (("below_cut", below), ("gap", gap)):
            for k in keys:
                if n_edge >= DECK_A_EDGE or len(items) >= DECK_A_MAX:
                    break
                n_edge += add(group, k)
        from . import atomic      # screen --judge: the rows moved to C on an inference go to your AI
        for k in atomic.review_keys(v_all):
            if len(items) >= DECK_A_MAX:
                break
            add("judge_demoted", k)
        items = items[:DECK_A_MAX]
    elif part == "B":
        if any(r.get("shortlist_tier") for r in rows):
            # the listed rows part A did not hold (exclude_keys: its items) and your AI has not answered on this
            # evidence (free()), in the same order: the to-confirm section first (the version after part A may have
            # moved rows between the sections, so part A's rows are named, not recomputed)
            later = review_order(rows, n=max_out)
        else:
            later = rows[DECK_A_TOP:max_out]
        for r in later:
            add("top", r["company_key"])
        strong = [e for e in extra.get("excluded_by_scope") or []
                  if e.get("l2_label") == "explicit" and (e.get("l2_p_explicit") or 0) >= E2_P_EXPLICIT]
        for e in strong[:REMOVED_MAX]:
            add("removed_by_scope", e["company_key"])
        n_below = min(BELOW_CUT_MAX, max(BELOW_CUT_MIN, int(removed_so_far) + 3))
        taken = 0
        for k in below:
            if taken >= n_below:
                break
            taken += add("below_cut", k)
        taken = 0
        for k in gap:
            if taken >= GAP_MAX:
                break
            taken += add("gap", k)
        items = items[:DECK_B_MAX]
    else:
        for k in list(followup_keys or [])[:FOLLOWUP_MAX]:
            add("followup", k)
    for i, it in enumerate(items, 1):
        it["n"] = i
    run_id = result.get("run_id")
    deck_id = f"adeck-{run_id}-{part}"
    return {"format": DECK_FORMAT, "deck_id": deck_id, "part": part, "blocking": part == "A", "run_id": run_id,
            "idea": result.get("idea"), "idea_en": result.get("idea_en"), "human_lang": human_lang,
            "facets": (sieve or {}).get("facets"), "facets_zh": (sieve or {}).get("facets_zh"),
            "criteria_en": criteria_en(result, sieve), "chips": chip_words(sieve),
            "held_questions": held_questions, "instructions_en": INSTRUCTIONS_EN, "answer_schema": ANSWER_SCHEMA,
            "record_command": f"jevscreen judge --deck {deck_id} --file <answers.json> --json",
            "skip_command": f"jevscreen judge --deck {deck_id} --skip --json",
            "licence_note": LICENCE_NOTE, "max_out": max_out, "items": items}


def deck_path(out_dir: str | Path, part: str) -> Path:
    return Path(out_dir) / f"agent_deck_{part}.json"


def write_deck(deck: dict[str, Any], out_dir: str | Path) -> Path:
    p = deck_path(out_dir, deck["part"])
    tmp = p.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(deck, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)
    return p


def parse_deck_id(deck_id: str) -> tuple[str, str]:
    """(run_id, part) of 'adeck-<run_id>-<part>'."""
    m = re.fullmatch(r"adeck-(scr-[0-9]{14}-[0-9a-z-]+?)-(A|B|F\d+)", str(deck_id or "").strip())
    if not m:
        raise AgentError(f"不认识这个核对编号：{deck_id}（形如 adeck-scr-20260927120000-abcdef-A）",
                         f"unknown review deck id {deck_id!r} (like adeck-scr-20260927120000-abcdef-A)")
    return m.group(1), m.group(2)


# ------------------------------------------------------------------------------------------------ answers

def _clip(text: Any, n: int) -> str | None:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return (t if len(t) <= n else t[: n - 1] + "…") if t else None


_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


def _in_lang(text: str | None, lang: str) -> str | None:
    """text when it is in the human's language (zh: has Chinese characters; en: has none), else None."""
    if not text:
        return None
    has = bool(_CJK.search(text))
    return text if has == (lang == "zh") else None


def parse_answers(deck: dict[str, Any], data: Any, sieve: dict[str, Any] | None = None
                  ) -> dict[int, dict[str, Any]]:
    """{item n: answer} of an answers file (§6.2). AgentError (exit 1, zh/en) for a malformed file, another deck, an
    unknown item, a bad v / level / chip; bad quote_ids downgrade the answer to unsure (quote_bad). why is needed in
    the human's language only (the other language falls back to the chip words). A missing item is 'not reviewed'."""
    if not isinstance(data, dict) or data.get("format") not in (ANSWERS_FORMAT, None) \
            or not isinstance(data.get("answers"), dict):
        raise AgentError("回答文件格式不对：要有 format、deck_id 和 answers", "the answers file needs format, deck_id and "
                                                                        "answers")
    if data.get("deck_id") != deck.get("deck_id"):
        raise AgentError(f"回答文件是给 {data.get('deck_id')} 的，不是 {deck.get('deck_id')}",
                         f"the answers are for {data.get('deck_id')!r}, not {deck.get('deck_id')!r}")
    items = {int(it["n"]): it for it in deck.get("items") or []}
    chips = set((deck.get("chips") or {}).keys()) or set(CHIPS)
    lang = deck.get("human_lang") or "zh"
    other = "en" if lang == "zh" else "zh"
    words = chip_words(sieve)
    out: dict[int, dict[str, Any]] = {}
    for key, a in data["answers"].items():
        try:
            n = int(str(key).strip())
        except ValueError:
            raise AgentError(f"回答编号 {key!r} 不是数字", f"answer key {key!r} is not a number") from None
        if n not in items:
            raise AgentError(f"核对里没有第 {n} 项", f"the deck has no item {n}")
        if not isinstance(a, dict):
            raise AgentError(f"第 {n} 项的回答要是一个对象", f"answer {n} must be an object")
        v = a.get("v")
        if v not in ("yes", "no", "unsure"):
            raise AgentError(f"第 {n} 项：v 只能是 yes / no / unsure", f"answer {n}: v must be yes, no or unsure")
        it = items[n]
        ans: dict[str, Any] = {"v": v, "quote_ids": [], "level": None, "chip": None, "unsure_kind": None}
        if v == "yes":
            if a.get("level") not in YES_LEVELS:
                raise AgentError(f"第 {n} 项：yes 要给 level（explicit 或 partial）",
                                 f"answer {n}: a yes needs level explicit or partial")
            ans["level"] = a["level"]
            if a.get("chip") not in (None, "a", "b"):
                raise AgentError(f"第 {n} 项：yes 不能带不要的类别 {a.get('chip')}",
                                 f"answer {n}: a yes cannot carry the no-chip {a.get('chip')!r}")
        if v == "no":
            if a.get("chip") not in chips or a.get("chip") in ("a", "b"):
                raise AgentError(f"第 {n} 项：no 要给这次核对允许的类别（{'、'.join(sorted(chips))}）",
                                 f"answer {n}: a no needs one of this deck's chips ({', '.join(sorted(chips))})")
            ans["chip"] = a["chip"]
        if v == "unsure":
            ans["unsure_kind"] = a.get("unsure_kind") if a.get("unsure_kind") in UNSURE_KINDS else "meaning"
        ids = a.get("quote_ids")
        ev = it.get("evidence") or {}
        valid = {int(i) for i, _ in (ev.get("sentences") or []) + ((ev.get("more") or {}).get("sentences") or [])}
        ok = (isinstance(ids, list) and 1 <= len(ids) <= QUOTE_IDS_MAX
              and all(isinstance(i, int) and not isinstance(i, bool) and i in valid for i in ids))
        if ok:
            ans["quote_ids"] = list(ids)       # an unsure answer may cite what it is unsure about (optional)
        elif v in ("yes", "no"):
            # the level your AI gave is kept (was_level): the question names its call in the page's words
            ans.update(v="unsure", unsure_kind="meaning", quote_bad=True, was=v, was_level=ans.get("level"),
                       level=None, chip=None)
        # the human reads why / short in their one language: text in the other language falls back to the chip or
        # level words (a zh text needs Chinese characters, Latin names inside are fine; an en text has none)
        why = _in_lang(_clip(a.get("why"), WHY_MAX[lang]), lang)
        ans[f"why_{lang}"] = why
        fallback = (words.get(ans["chip"] or "", {}).get(other) if ans.get("chip") else None) or (
            {"zh": "明确符合", "en": "clearly fits"}[other] if ans.get("level") == "explicit" else
            {"zh": "相关", "en": "related"}[other] if ans.get("level") == "partial" else
            {"zh": "拿不准", "en": "not sure"}[other])
        ans[f"why_{other}"] = fallback
        if not why:
            ans[f"why_{lang}"] = (words.get(ans["chip"] or "", {}).get(lang) if ans.get("chip") else None) or (
                {"zh": "明确符合", "en": "clearly fits"}[lang] if ans.get("level") == "explicit" else
                {"zh": "相关", "en": "related"}[lang] if ans.get("level") == "partial" else
                {"zh": "拿不准", "en": "not sure"}[lang])
        ev_lang = (it.get("evidence") or {}).get("lang")
        if a.get("quote_tr") and ev_lang and ev_lang != lang:
            ans["quote_tr"] = _clip(a.get("quote_tr"), QUOTE_TR_MAX)
        if it.get("held_sid"):
            ans["in_group"] = a.get("in_group") if isinstance(a.get("in_group"), bool) else None
            ans[f"short_{lang}"] = _in_lang(_clip(a.get("short"), SHORT_MAX[lang]), lang)
        out[n] = ans
    return out


# ------------------------------------------------------------------------------------------------ escalations

ESC_ZH = {
    "E1": "{name}（第 {rank} 名）：摘录的意思你的 AI 拿不准{why}。{quote}要不要？",
    "E1b": "{name}（第 {rank} 名）：你的 AI 判断{was}，但没指出摘录里哪句能说明{why}。{quote}要不要？",
    "E2": "{name}（第 {rank} 名）：系统判它符合，你的 AI 认为不符合{why}。{quote}要不要？",
    "E3": "{name}：系统没确认，你的 AI 认为该加进来{why}。{quote}要不要？",
    "E4": "{name}：和你之前的回答不一致——你说过{prev}，你的 AI 认为{now}（{why0}）。按哪个？",
}
# the same questions about a row of the to-confirm section (the page shows it without a number: the section is
# named, and a yes puts it into the confirmed list), and your AI's no on a row of the confirmed list (moved to the
# to-confirm section until the human decides)
ESC_ZH_CONFIRM = {
    "E1": "{name}（待核对）：摘录的意思你的 AI 拿不准{why}。{quote}要不要放进确认名单？",
    "E1b": "{name}（待核对）：你的 AI 判断{was}，但没指出摘录里哪句能说明{why}。{quote}要不要放进确认名单？",
    "E2": "{name}（待核对）：系统判它符合，你的 AI 认为不符合{why}。{quote}要不要放进确认名单？",
}
ESC_ZH_MAIN_NO = "{name}（确认名单第 {rank} 家，已先移到待核对）：系统判它符合，你的 AI 认为不符合{why}。{quote}要不要放回确认名单？"
ESC_EN = {
    "E1": "{name} (#{rank}): your AI is not sure what the excerpt means{why}. {quote}Keep it?",
    "E1b": "{name} (#{rank}): your AI says {was} but did not point to the sentence that shows it{why}. {quote}Keep it?",
    "E2": "{name} (#{rank}): the system says it fits, your AI says it does not{why}. {quote}Keep it?",
    "E3": "{name}: the system did not confirm it, your AI says it belongs{why}. {quote}Keep it?",
    "E4": "{name}: this differs from your earlier answer: you said {prev}, your AI says {now} ({why0}). Which one?",
}
ESC_EN_CONFIRM = {
    "E1": "{name} (to confirm): your AI is not sure what the excerpt means{why}. {quote}Add it to the confirmed list?",
    "E1b": "{name} (to confirm): your AI says {was} but did not point to the sentence that shows it{why}. {quote}Add "
           "it to the confirmed list?",
    "E2": "{name} (to confirm): the system says it fits, your AI says it does not{why}. {quote}Add it to the "
          "confirmed list?",
}
ESC_EN_MAIN_NO = ("{name} (confirmed list #{rank}, moved to confirm for now): the system says it fits, your AI says "
                  "it does not{why}. {quote}Put it back in the confirmed list?")
# E1b's "your AI says {was}": page.STRINGS['en'] words as a clause
WAS_EN = {"agent_yes": "it fits", "agent_partial": "it is partly related", "agent_no": "it does not fit"}
QUOTE_ZH, QUOTE_EN = "摘录：「{q}」{tr}。", "Excerpt: \"{q}\"{tr}. "
FALLBACK_SENTS = 2              # an escalation without cited sentences quotes the item's first sentences
DEFAULT_WHY = {"zh": ("拿不准", ""), "en": ("not sure", "")}
TR_ZH, TR_EN = "（译文：{t}）", " (translation: {t})"


def row_ctx(item: dict[str, Any], row: dict[str, Any] | None, *, max_out: int, cut_score: float | None
            ) -> dict[str, Any]:
    """The facts escalation needs about the row an answer is for (stored with the verdict)."""
    from . import screen
    r = row or {}
    sysd = item.get("system") or {}
    in_top = item.get("rank") is not None and int(item["rank"]) <= max_out
    would = None
    if cut_score is not None and r.get("l2_label") not in screen.L2_VERIFIED:
        s = screen.score_of("partial", r.get("l1_p_core"), r.get("market_cap_usd"), r.get("l2_evidence"))
        would = s > cut_score
    return {"group": item.get("group"), "rank": item.get("rank"), "in_top": in_top,
            # the page section the row was in (main / to_confirm; None: not listed or a single padded list): the
            # question names the section, not a rank the page does not show
            "section": item.get("section"),
            "l2_label": sysd.get("label"), "p_explicit": sysd.get("p_explicit"),
            "mentions_idea": item.get("mentions_idea"), "would_list": would}


def classify(ans: dict[str, Any], ctx: dict[str, Any], human: dict[str, Any] | None = None) -> tuple[str, str | None]:
    """(state, escalation code) of one answer (§6.5): E4 a conflict with a human answer; E1 unsure about the
    meaning (or bad quote ids) on a listed / gap / below-cut row; E2 a no on a row the system is confident about;
    E3 an explicit yes on an unverified row that would make the list (only an explicit yes promotes a row: a partial
    one leaves it to confirm, so it is applied without asking). Thin evidence is a badge, never an escalation; weak
    disagreements are applied without asking."""
    v = ans.get("v")
    if human:
        hw = human.get("want")
        if (hw in ("explicit", "partial") and v == "no") or (hw == "no" and v == "yes"):
            return "escalated", "E4"
    if v == "unsure":
        thin = ans.get("unsure_kind") == "thin" or ctx.get("mentions_idea") is False
        if not thin and (ctx.get("in_top") or ctx.get("group") in ("gap", "below_cut")):
            return "escalated", "E1"
        return "applied", None
    if v == "no":
        if ctx.get("l2_label") == "explicit" and float(ctx.get("p_explicit") or 0.0) >= E2_P_EXPLICIT \
                and ctx.get("mentions_idea"):
            return "escalated", "E2"
        return "applied", None
    if v == "yes" and ans.get("level") == "explicit" and ctx.get("would_list") and ans.get("quote_ids"):
        return "escalated", "E3"
    return "applied", None


def verdict_of(item: dict[str, Any], ans: dict[str, Any], *, deck: dict[str, Any], ctx: dict[str, Any],
               state: str, code: str | None, answers_sha: str, agent_name: str | None,
               now: str | None = None) -> dict[str, Any]:
    ev = item.get("evidence") or {}
    more = ev.get("more") or {}
    shown = dict((int(i), x) for i, x in (ev.get("sentences") or []) + (more.get("sentences") or []))
    ids = ans.get("quote_ids") or []
    more_ids = {int(i) for i, _ in more.get("sentences") or []}
    extra = {}
    if ids:
        # what was cited, as your AI was shown it: evidence.more sentences are not in the L2 text
        extra["quotes"] = {str(i): shown[i] for i in ids if i in shown}
    if more_ids & set(ids):
        extra["more_source"] = {k: more.get(k) for k in ("source", "form", "filing_date", "lang")}
    if item.get("review_sha"):
        extra["review_sha"] = item["review_sha"]
    return {**extra, "company_key": item["company_key"], "security_id": item.get("security_id"), "name": item.get("name"),
            "name_zh": item.get("name_zh"), "evidence_sha": item.get("evidence_sha"), "v": ans["v"],
            "level": ans.get("level"), "chip": ans.get("chip"), "unsure_kind": ans.get("unsure_kind"),
            "quote_bad": bool(ans.get("quote_bad")), "was": ans.get("was"),
            "was_level": ans.get("was_level"), "quote_ids": ans.get("quote_ids") or [],
            "why_zh": ans.get("why_zh"), "why_en": ans.get("why_en"), "quote_tr": ans.get("quote_tr"),
            "held_sid": item.get("held_sid"), "held_kind": item.get("held_kind"), "in_group": ans.get("in_group"),
            "short_zh": ans.get("short_zh"), "short_en": ans.get("short_en"), "state": state, "escalation": code,
            "mentions_idea": item.get("mentions_idea"), "ctx": ctx, "deck_id": deck.get("deck_id"),
            # what your AI read (annual report or profile): the chat's cumulative split counts it
            "evidence_kind": (item.get("evidence") or {}).get("kind"),
            "run_id": deck.get("run_id"), "item_n": item["n"], "agent": agent_name, "answers_sha": answers_sha,
            "answered_at": now or now_iso()}


def escalation_item(v: dict[str, Any], cid: str, *, text: str | None, lang_ev: str | None, sieve: dict[str, Any]
                    | None, relayed_top: int = 10, human: dict[str, Any] | None = None) -> dict[str, Any]:
    """The question of one escalated verdict (one template per reason, zh and en)."""
    from . import calib, page
    code = v.get("escalation") or "E1"
    ctx = v.get("ctx") or {}
    quote = cited_text(text, v.get("quote_ids") or [], v.get("quotes"))
    name_en = v.get("name") or v.get("security_id")
    name_zh = v.get("name_zh") or name_en
    out = {"id": "confirm_company", "cid": cid, "reason": code, "ask_human": True, "optional": True,
           "security_id": v.get("security_id"), "company_key": v.get("company_key"), "name": v.get("name"),
           "name_zh": v.get("name_zh"), "rank": ctx.get("rank"),
           # asked in chat: a row of the relayed list (with the sections: the confirmed list only; a to-confirm
           # row is on the page's question box, not in chat)
           "in_relayed_top": ctx.get("rank") is not None and int(ctx["rank"]) <= relayed_top
           and ctx.get("section") in (None, "main"),
           "section": ctx.get("section"),
           "tokens": {"yes": f"{cid}=yes", "no": f"{cid}=no", "unsure": f"{cid}=?"}}
    prev_zh = calib.answer_words_zh((human or {}).get("want"), (human or {}).get("chip"))
    prev_en = calib.answer_words_en((human or {}).get("want"), (human or {}).get("chip"))
    now_zh = {"yes": "要", "no": "不要", "unsure": "拿不准"}[v["v"]]
    now_en = {"yes": "keep", "no": "drop", "unsure": "not sure"}[v["v"]]
    cited = bool(v.get("quote_ids")) and bool(quote)
    if not quote and text:
        # nothing cited (an unsure answer, or bad quote ids): the item's first sentences, so the human has
        # something to judge from
        sents = [s for _i, s in sentences(text)[:FALLBACK_SENTS]]
        q = " ".join(sents)
        quote = q if len(q) <= QUOTE_MAX else q[: QUOTE_MAX - 1].rstrip() + "…"
    if quote:
        # a stored profile's internal source tag ("[financedatabase_local] ...") is not for the human
        quote = re.sub(r"\[(?:[a-z0-9]+_)+[a-z0-9]+\]\s*", "", quote).strip()
    tr = v.get("quote_tr") if cited else None      # a translation belongs to the cited sentences only
    if code == "E1" and v.get("quote_bad") and v.get("was") in ("yes", "no"):
        code = "E1b"
    # your AI's call in the page's words (page.agent_key): a partial yes is 部分相关 / partly related, never 符合
    was_key = page.agent_key({"v": v.get("was"), "level": v.get("was_level")}) \
        if v.get("was") in ("yes", "no") else None
    was_zh = page.STRINGS["zh"][was_key] if was_key else ""
    was_en = WAS_EN.get(was_key or "", "")
    # the templates end the why with their own full stop: one of the AI's own would double it ("。。")
    why_zh = (v.get("why_zh") or "").rstrip().rstrip("。．.！!")
    why_en = (v.get("why_en") or "").rstrip().rstrip(".!")
    sec = ctx.get("section")
    tz, te = ESC_ZH[code], ESC_EN[code]
    if sec == "to_confirm" and code in ESC_ZH_CONFIRM:
        tz, te = ESC_ZH_CONFIRM[code], ESC_EN_CONFIRM[code]
    elif sec == "main" and code == "E2":
        tz, te = ESC_ZH_MAIN_NO, ESC_EN_MAIN_NO
    out["question_zh"] = tz.format(
        name=name_zh, rank=ctx.get("rank") or "?", was=was_zh, prev=prev_zh, now=now_zh, why0=why_zh or now_zh,
        why="" if why_zh in DEFAULT_WHY["zh"] else f"——{why_zh}",
        quote=QUOTE_ZH.format(q=quote, tr=TR_ZH.format(t=tr) if tr and lang_ev != "zh" else "") if quote else "")
    out["question_en"] = te.format(
        name=name_en, rank=ctx.get("rank") or "?", was=was_en, prev=prev_en, now=now_en, why0=why_en or now_en,
        why="" if why_en in DEFAULT_WHY["en"] else f": {why_en}",
        quote=QUOTE_EN.format(q=quote, tr=TR_EN.format(t=tr) if tr and lang_ev != "en" else "") if quote else "")
    return out


# ------------------------------------------------------------------------------------------------ held answers

def resolve_held(doc: dict[str, Any], kind: str, answer: str, *, human: dict[str, dict[str, Any]] | None = None
                 ) -> dict[str, Any]:
    """doc' after the human answered the scope question of `kind` (§6.4): 不要 -> the side-V rows go by the scope
    rule and the other held answers apply as usual; 要 -> held no's with the value's chip are not applied (the
    human's category wins), the rest apply; 不确定 / skipped -> every held answer applies as usual (weak ones
    applied, strong ones escalated)."""
    from . import scope
    fam, _, val = kind.partition(".")
    chip = scope.chip_of(fam, val)
    d = copy.deepcopy(doc)
    for v in d.get("verdicts") or []:
        if v.get("state") != "held" or v.get("held_kind") != kind:
            continue
        if answer == "yes" and v.get("v") == "no" and v.get("chip") == chip:
            v["state"], v["escalation"] = "not_applied", None
            continue
        h = (human or {}).get(v.get("company_key")) or (human or {}).get(v.get("security_id"))
        v["state"], v["escalation"] = classify(v, v.get("ctx") or {}, h)
    return d


def open_kinds(review: dict[str, Any] | None) -> set[str]:
    """The kinds of the questions still open in a review (asked, not answered or skipped)."""
    ans = (review or {}).get("answered") or {}
    return {q["kind"] for q in (review or {}).get("questions") or [] if q["sid"] not in ans}


# ------------------------------------------------------------------------------------------------ review.json

def review_path(out_dir: str | Path) -> Path:
    return Path(out_dir) / "review.json"


def load_review(out_dir: str | Path | None) -> dict[str, Any]:
    if not out_dir:
        return {}
    try:
        data = json.loads(review_path(out_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and data.get("format") == REVIEW_FORMAT else {}


def save_review(out_dir: str | Path, review: dict[str, Any]) -> Path:
    p = review_path(out_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps({**review, "format": REVIEW_FORMAT}, ensure_ascii=False, indent=1, sort_keys=True),
                   encoding="utf-8")
    os.replace(tmp, p)
    return p


def sha_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


def human_pins(sieve: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    from . import calib
    """{company_key or security_id: example} of every human decision: pins (card / sieve pin / escalation /
    override) and the should_pass / should_fail checks (want no for should_fail)."""
    out = dict(calib.pins(sieve))
    for ex in (sieve or {}).get("examples") or []:
        if isinstance(ex, dict) and ex.get("source") == "sieve":
            for k in {ex.get("company_key"), ex.get("security_id")} - {None}:
                out.setdefault(k, ex)
    return out
