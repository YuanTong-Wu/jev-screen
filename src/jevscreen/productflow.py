"""`eval run --product-flow`: screen each eval idea the way the product lists it, not the bare system.

The eval used to screen with `sieve: none` (no facets, no facet layer, no idea-wording defaults, no tiers), so it
measured a list no user sees. With --product-flow each idea gets, from its own text only (no human, no AI, no label):

- facets (category, target, type) derived deterministically from the idea's English sentence with
  constraints.derive (the same reading the --l2-constraints lever uses): the end market or place it names is the
  target; the words before it, stripped of framing ("Listed suppliers of", "companies that make"), the category;
- the idea-wording defaults the product records when the user's AI passes `--facets implied_no` (scope source
  idea_wording), chosen by fixed rules (IMPLIED below), but always with effect **demote**: a row the facet layer reads
  as that kind moves down, it is never removed;
- the facet layer on (screen facet_scan=True: role / scope / geo choice questions over the same L2 text; cached;
  about $0.01 an idea), the high-confidence shortlist tiers on, and your AI's review layer off (the eval measures the
  system, never a review file that happens to exist for the idea).

The sieve is built in memory and passed to screen() as a dict: nothing is written to <home>/sieves, and the L2
question is unchanged (facets change it only together with rules, and this sieve has none), so a --from-run arm
pays only for the facet questions.

Pure helpers: no store access, no network.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

FIXED_NOW = "2026-01-01T00:00:00+00:00"      # the in-memory sieve's timestamps: the same idea gives the same sieve

# framing words around the product in an idea sentence (removed from the category, never from the idea)
_LEAD = re.compile(
    r"^(?:(?:the|all)\s+)?(?:(?:listed|public|publicly listed|[a-z]+-listed)\s+)?"
    r"(?:(?:companies|firms|groups|suppliers|vendors|providers|makers|manufacturers|producers|developers|designers)"
    r"(?:\s+and\s+(?:suppliers|vendors|providers|makers|manufacturers|producers|developers|designers))?\s+"
    r"(?:(?:that|which|who)\s+)?(?:of\s+)?)?", re.I)
_VERBS = re.compile(
    r"^(?:(?:own|owns|owning|operate|operates|operating|make|makes|making|sell|sells|selling|provide|provides|"
    r"providing|supply|supplies|supplying|develop|develops|developing|design|designs|designing|produce|produces|"
    r"producing|manufacture|manufactures|manufacturing|build|builds|building|offer|offers|offering)\b"
    r"(?:\s*,\s*(?:or|and)\s+|\s*,\s*|\s+(?:or|and)\s+)?)+\s*(?:their\s+own\s+)?", re.I)
_TRAIL = re.compile(
    r"\s+(?:companies|firms|makers|manufacturers|producers|suppliers|vendors|providers|shipbuilders|miners|"
    r"and producers|equipment makers)$", re.I)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip(" .,;:")


def category_of(idea_en: str) -> str | None:
    """The product words of an English idea sentence: the part before the end-market clause, without the framing
    words around it and without a listing place ('Korea-listed', a leading demonym). None when nothing is left."""
    from . import constraints
    s = _clean(idea_en)
    if not s:
        return None
    tail = constraints._TAIL_RE.search(s)
    head = _clean(s[:tail.start()] if tail else s)
    for m in constraints._PLACE_RE.finditer(head):
        if constraints._is_listing(m, head):          # the companies' own listing / nationality: the market filter
            head = _clean(head[:m.start()] + head[m.end():])
            break
    head = _clean(_LEAD.sub("", head, count=1))
    head = _clean(_VERBS.sub("", head, count=1))
    prev = None
    while prev != head:
        prev = head
        head = _clean(_TRAIL.sub("", head))
    return head or None


def facets_of_idea(idea_en: str | None) -> dict[str, str] | None:
    """{category, target, type} from the idea's English sentence (constraints.derive: its end market, else its
    place), or None when it names no target (the facet layer then asks the generic role question only)."""
    from . import calib, constraints
    en = _clean(idea_en or "")
    if not en:
        return None
    cons = {c["kind"]: c["text"] for c in constraints.derive(en)}
    cat = category_of(en)
    if not cat:
        return None
    if cons.get("geography") and not cons.get("end_market"):
        return {"category": cat, "target": cons["geography"], "type": "geography"}
    if cons.get("end_market"):
        f = {"category": cat, "target": cons["end_market"]}
        return {**f, "type": calib.facet_type(f) or "technology"}
    return None


# (family.value, when) of the idea-wording defaults, in priority order (scope.IMPLIED_NO_MAX of them at most):
# - role.target_only: a technology idea that names its target ("hands for humanoid robots") does not want the makers
#   of the target that do not offer the product;
# - role.buyer / role.holding: an idea about a product wants the companies that offer it, not the ones that buy it
#   or only hold a stake in its makers.
IMPLIED = (("role.target_only", "technology_target"), ("role.buyer", "always"), ("role.holding", "always"))


def implied_no(idea_en: str | None, facets: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """The `--facets implied_no` entries the rules above give ({key, because}: `because` is words of the idea)."""
    from . import scope
    en = _clean(idea_en or "")
    out: list[dict[str, str]] = []
    if not en:
        return out
    kind = (facets or {}).get("type")
    for key, when in IMPLIED:
        if len(out) >= scope.IMPLIED_NO_MAX:
            break
        if when == "technology_target":
            if kind != "technology" or not (facets or {}).get("target"):
                continue
            because = "for " + str(facets["target"])
            if because.casefold() not in en.casefold():
                because = str(facets["target"])
        else:
            because = category_of(en) or en
        out.append({"key": key, "because": because})
    return out


def sieve_for(data: Mapping[str, Any]) -> dict[str, Any]:
    """The in-memory sieve of one eval idea: facets from its idea_en (else its idea), the idea-wording defaults as
    scope answers with effect demote (never remove). Deterministic: the same idea gives the same sieve."""
    from . import calib, scope
    idea = str(data.get("idea") or "").strip()
    en = str(data.get("idea_en") or "").strip() or idea
    sv = calib.new_sieve(idea, now=FIXED_NOW)
    facets = facets_of_idea(en)
    if facets:
        sv["facets"] = dict(facets)
    # scope.implied_defaults checks `because` against the idea text: give it both sentences
    defaults, _notes = scope.implied_defaults(f"{idea}\n{en}", implied_no(en, facets), sv)
    entries = scope.default_entries(defaults, sv, fsha=scope.facets_sha(sv), run_id=None)
    for e in entries:
        e["effect"] = "demote"              # the product-flow eval only ever moves a row down
    if entries:
        sv = scope.record(sv, entries, now=FIXED_NOW)
    return sv


def summary(sv: Mapping[str, Any]) -> dict[str, Any]:
    """What an eval score records of the sieve: facets and the default kinds (all demote)."""
    return {"facets": sv.get("facets"),
            "defaults": [f"{e['family']}.{e['value']}" for e in sv.get("scope_answers") or []
                         if isinstance(e, dict) and e.get("source") == "idea_wording"],
            "effect": "demote"}


def screen_kwargs(data: Mapping[str, Any]) -> dict[str, Any]:
    """The screen() arguments of the product flow for one idea (they win over the lever flags)."""
    return {"sieve": sieve_for(data), "facet_scan": True, "shortlist": True, "agent_layer": False}
