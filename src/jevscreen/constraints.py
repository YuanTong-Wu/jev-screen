"""The L2 constraint lever (`screen --l2-constraints`, experiment queue #3 / #6; default OFF).

An idea often names more than a product: the end market it must serve ("... for grid-scale battery storage"), a
place ("... for Southeast Asian consumers") or a role ("suppliers of ..."). Layer 2 asks whether the text states the
product; a company that makes the product for another market, or only has a generic capability, can still come out
`explicit`. With the lever on, the idea's explicit constraints are derived here (free, deterministic) and ONE cheap
Jev choice question is asked over the SAME L2 text, only for the L2-explicit companies. An explicit row whose text
does not state every constraint becomes partial (`l2_constraint`: missing / unclear), so it can never be in the
high-confidence tier.

Design (cheaper and cache-safe): L1 and the L2 question are untouched (same item keys, cached answers reused); the
extra question costs about one L2 read of the explicit rows (< $0.01 on a typical run). A row the budget could not
check keeps its explicit label (`unchecked`) and the run is partial.

Pure helpers: no store access, no network.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from typing import Any, Iterable

KEY = "constraint"
KINDS = ("end_market", "geography", "role")
LABELS = ("met", "product_only", "unclear")
BAND = (0.40, 0.75)          # read-0 probability of 'met' in this band -> 2 more reads, decided on the mean
BAND_READS = (1, 2)
BAND_SHARE = 0.30            # worst-case share of items in the band (budget estimate)
READ_MAX = 150               # explicit rows read (score order)
ITEM_USD = 0.00005           # dry-run estimate per item (as the facet layer)

INSTRUCTIONS = (
    'Using ONLY state.text, decide whether it states that state.issuer offers what the investment idea "{idea}" '
    "describes WITH the idea's explicit constraints: {constraints}. Do not use outside knowledge. The company name "
    "alone is not evidence.")

# Place names and demonyms (lower case) -> the name used in the question. A place written as a listing ("Korea-listed")
# or as the first word of the idea (the companies' nationality, "Japanese ... makers") is enforced by the market
# filters, not by the text, and is not a constraint here.
PLACES: dict[str, str] = {
    "southeast asian": "Southeast Asia", "southeast asia": "Southeast Asia", "south-east asia": "Southeast Asia",
    "asean": "Southeast Asia", "the united states": "the United States", "united states": "the United States",
    "u.s.": "the United States", "american": "the United States",
    "china": "China", "chinese": "China", "japan": "Japan", "japanese": "Japan", "korea": "Korea",
    "korean": "Korea", "south korea": "Korea", "taiwan": "Taiwan", "taiwanese": "Taiwan", "india": "India",
    "indian": "India", "europe": "Europe", "european": "Europe", "latin america": "Latin America",
    "latin american": "Latin America", "africa": "Africa", "african": "Africa", "middle east": "the Middle East",
    "indonesia": "Indonesia", "indonesian": "Indonesia", "vietnam": "Vietnam", "vietnamese": "Vietnam",
    "thailand": "Thailand", "thai": "Thailand", "philippines": "the Philippines", "philippine": "the Philippines",
    "malaysia": "Malaysia", "malaysian": "Malaysia", "singapore": "Singapore", "brazil": "Brazil",
    "brazilian": "Brazil", "mexico": "Mexico", "mexican": "Mexico", "canada": "Canada", "canadian": "Canada",
    "australia": "Australia", "australian": "Australia", "germany": "Germany", "german": "Germany",
    "hong kong": "Hong Kong", "north america": "North America", "north american": "North America",
}
_PLACE_RE = re.compile(r"(?<![A-Za-z])(" + "|".join(sorted((re.escape(p) for p in PLACES), key=len, reverse=True))
                       + r")(-listed)?(?![A-Za-z])", re.I)
# the clause that names the end market / customer / place: "for X", "serving X", "used in X"
_TAIL_RE = re.compile(r"\b(?:for|serving|used (?:in|by|for)|sold to)\s+(.+)$", re.I)
_TAIL_CUT = re.compile(r"\s*(?:\(|,|;|\bincluding\b|\bsuch as\b|\bespecially\b)")
# words that say nothing specific about an end market
GENERIC = frozenset("""customer customers client clients consumer consumers user users business businesses company
companies enterprise enterprises merchant merchants people others other them it third third-party parties party and or
the a an of in to their its local global worldwide use""".split())
# role words -> what the company itself must do
ROLES: tuple[tuple[str, str], ...] = (
    (r"\bown(?:s|ing)?\b.*\boperat|\boperat(?:e|es|ing|ors?)\b", "owns or operates it"),
    (r"\bminers?\b|\bmining\b", "mines or produces it"),
    (r"\bdesigners?\b|\bdevelop(?:ers?|ing)\b", "designs, develops or makes it"),
    (r"\bmakers?\b|\bmanufactur(?:ers?|ing)\b|\bproducers?\b|\bshipbuilders?\b|\bODM\b|\bOEM\b|"
     r"\bcontract manufacturers?\b|\bbuild(?:ers?|ing)\b|\bmake\b", "makes or builds it"),
    (r"\bsuppliers?\b|\bvendors?\b|\bproviders?\b|\bprovid(?:e|es|ing)\b|\bsell(?:s|ing)?\b",
     "supplies or provides it"),
)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(" .,;:")


def _is_listing(m: re.Match, s: str) -> bool:
    """A place written as a listing ("Korea-listed") or leading the idea (the companies' own nationality)."""
    if m.group(2):
        return True
    head = s[:m.start()].strip().lower()
    return head in ("", "listed", "public", "the")


def _meaningful(text: str) -> bool:
    rest = _PLACE_RE.sub(" ", text)
    words = [w for w in re.split(r"[^A-Za-z0-9.+/-]+", rest.lower()) if w]
    return any(w not in GENERIC for w in words)


def _given(given: Iterable[Any]) -> list[dict[str, str]]:
    out = []
    for c in given or ():
        if isinstance(c, dict) and c.get("kind") in KINDS and str(c.get("text") or "").strip():
            out.append({"kind": c["kind"], "text": _clean(str(c["text"]))})
    return out


def derive(idea_en: str | None, facets: dict[str, Any] | None = None,
           given: Iterable[Any] | None = None) -> list[dict[str, str]]:
    """[{kind, text}] of the idea's explicit constraints, in KINDS order. `given` (a sieve's `constraints`, written
    by the user's AI) wins; else the sieve's facets (target: a place for a geography idea, an end market for the
    other types); else a deterministic reading of idea_en. [] when the idea names nothing beyond the product."""
    if given:
        g = _given(given)
        if g:
            return g
    f = facets if isinstance(facets, dict) else None
    if f and f.get("target"):
        kind = "geography" if f.get("type") == "geography" else "end_market"
        return [{"kind": kind, "text": _clean(str(f["target"]))}]
    s = _clean(str(idea_en or ""))
    if not s:
        return []
    found: dict[str, str] = {}
    geo = None
    for m in _PLACE_RE.finditer(s):
        if not _is_listing(m, s):
            geo = PLACES[m.group(1).lower()]
            break
    tail = _TAIL_RE.search(s)
    head = s[:tail.start()] if tail else s
    if tail:
        t = _clean(_TAIL_CUT.split(tail.group(1), maxsplit=1)[0])
        if t and _meaningful(t):
            found["end_market"] = t
    if geo:
        found["geography"] = geo
    # every role the head names: "make or sell" keeps both (the company itself makes it OR supplies it)
    roles = [text for pat, text in ROLES if re.search(pat, head, re.I)]
    if roles:
        found["role"] = " or ".join(dict.fromkeys(roles))
    return [{"kind": k, "text": found[k]} for k in KINDS if k in found]


def _phrase(c: dict[str, str]) -> str:
    if c["kind"] == "end_market":
        return f"it is for {c['text']}"
    if c["kind"] == "geography":
        return f"it is offered in or to {c['text']}"
    return f"the company itself {c['text']}"


def criteria(cons: list[dict[str, str]]) -> dict[str, str]:
    every = "; ".join(_phrase(c) for c in cons)
    anyof = " or ".join(_phrase(c) for c in cons)
    return {
        "met": f"The text states that the company offers what the idea describes and states each of these: {every}.",
        "product_only": ("The text describes a related product or service but does not state that " + anyof
                         + ": it names another end market, place or role, or only a generic capability, an "
                           "adjacent product, or a plan or outlook."),
        "unclear": "The text does not say enough to tell.",
    }


def build_question(idea: str, idea_en: str | None, cons: list[dict[str, str]]):
    from . import screen
    Question, _ = screen._jev_types()
    text = "; ".join(_phrase(c) for c in cons)
    return Question(key=KEY, instructions=INSTRUCTIONS.format(idea=screen.question_idea(idea, idea_en),
                                                              constraints=text),
                    criteria=criteria(cons))


def question_sha(q) -> str:
    body = json.dumps({"key": q.key, "instructions": q.instructions, "criteria": q.criteria}, sort_keys=True,
                      ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def replace_read(q, r: int):
    return dataclasses.replace(q, read=r)


def p_met(probs: dict[str, Any] | None) -> float:
    v = (probs or {}).get("met")
    return float(v) if isinstance(v, (int, float)) else 0.0


def in_band(probs: dict[str, Any] | None) -> bool:
    return BAND[0] <= p_met(probs) <= BAND[1]


def aggregate(reads: list[dict[str, Any]]) -> dict[str, Any] | None:
    """{label, p, n, probs} from the ok reads of one item: the mean per label, label = argmax (ties: met first)."""
    ok = [{k: float(v) for k, v in (r.get("probs") or {}).items() if k in LABELS and isinstance(v, (int, float))}
          for r in reads if r.get("status") == "ok"]
    ok = [p for p in ok if p]
    if not ok:
        return None
    mean = {v: sum(p.get(v, 0.0) for p in ok) / len(ok) for v in LABELS}
    best = max(LABELS, key=lambda v: (round(mean[v], 9), -LABELS.index(v)))
    return {"label": best, "p": round(mean[best], 4), "n": len(ok),
            "probs": {v: round(x, 4) for v, x in mean.items() if x > 0}}


def decide(l2_label: str | None, agg: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """(label used for ranking, constraint state) of one row: an explicit row keeps explicit only when its text
    states the constraints (met); missing / unclear make it partial; no answer (budget, failure) keeps it explicit
    and marks it unchecked. Other labels pass through with no state."""
    if l2_label != "explicit":
        return l2_label, None
    if not agg:
        return "explicit", "unchecked"
    if agg.get("label") == "met":
        return "explicit", "met"
    return "partial", "missing" if agg.get("label") == "product_only" else "unclear"


def demoted(state: str | None) -> bool:
    """Whether a row's constraint state moved it from explicit to partial."""
    return state in ("missing", "unclear")


def ev_probs(p_explicit: float | None, p_partial: float | None, state: str | None
             ) -> tuple[float | None, float | None]:
    """(p_explicit, p_partial) that `--rank ev` scores a row with: a demoted row's explicit mass counts as partial,
    so the demotion moves its score as it moves its label."""
    if not demoted(state) or p_explicit is None:
        return p_explicit, p_partial
    return 0.0, float(p_partial or 0.0) + float(p_explicit)


def estimate_items(n_explicit: int) -> float:
    """Dry-run upper bound of the layer."""
    return round(min(READ_MAX, n_explicit) * ITEM_USD * (1 + 2 * BAND_SHARE), 6)
