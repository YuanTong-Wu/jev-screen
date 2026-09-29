"""The typed atomic judgement layer (`screen --judge atomic3|single10`, structural plan step 2; default OFF).

Layer 2 gives one explicit / partial label, which mixes three different mistakes: the wrong product (a component,
a neighbouring product), the wrong role (a buyer, a distributor, a minority holder) and the wrong target (another
end market or place). With the layer on, every L2-verified row is read again over the SAME L2 text with typed
choice questions, and a fixed rule combines the answers into tiers A / B / C that order the list before the score:

- arm `atomic3`: three questions. product (own / component / assembly_only / adjacent / none), role (supplier /
  buyer_user / upstream / channel / minority_holder / other_link / unclear; a consolidated subsidiary counts as the
  company itself) and, when the idea names a target, target (named / one_of_several / other_only / not_stated; the
  target phrase is also given in head-noun form);
- arm `single10`: one ten-class question with the same meaning (about a third of the cost).

The tier rule (tier_of, fixed; never tuned per idea):
- C (moved to the end, never removed): POSITIVE counter-evidence at mean probability >= P_DEMOTE: product
  component / assembly_only / adjacent, role buyer_user / upstream / channel / minority_holder / other_link, or
  target other_only. "none" / "unclear" / "not_stated" never demote. A C row carries judge_inferred (an inference,
  shown as such) and goes to the user's AI review (review.build_deck part A, group judge_demoted);
- A: L2 explicit and product own, role supplier and (no target in the idea, or target named / one_of_several);
- B: everything else, including a row the layer could not read (judge_state unchecked: budget or service)
  and a row beyond the READ_MAX rows it reads (judge_state not_read: the cap, so the run is not partial).
A human yes pin, or the user's AI's applied yes, lifts a row out of C (order_rank); a human partial pin is B.
Within a tier the old score orders the rows, so with the layer off (no judge_tier) the order is exactly as before.

Cache-safe: L1 and the L2 question are untouched; the items are the L2 items (same text), the questions are
deterministic (question_sha). Budgeted: estimate = cache-aware read-0 estimate x band_factor (1 + re-reads x
BAND_SHARE; an estimate, not a bound), skipped with a note (no tiers at all) when the budget cannot cover it; a
band re-read pass that no longer fits the layer's budget is skipped with a note (decided on read 0), so the layer
does not stop half-way through a family on its own re-reads.

Pure helpers plus run_layer (Jev calls through the screen's client factory). No store access.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from typing import Any, Iterable

MODES = ("atomic3", "single10")
FAMILIES = ("product", "role", "target")
KEYS = {"product": "judge_product", "role": "judge_role", "target": "judge_target", "single": "judge_single"}
KEY_FAMILY = {v: k for k, v in KEYS.items()}

PRODUCT = ("own", "component", "assembly_only", "adjacent", "none")
ROLE = ("supplier", "buyer_user", "upstream", "channel", "minority_holder", "other_link", "unclear")
TARGET = ("named", "one_of_several", "other_only", "not_stated")
SINGLE = ("supplier_named", "supplier_one_of_several", "supplier_target_unstated", "supplier_other_target",
          "component", "assembly_only", "adjacent", "buyer_user", "other_role", "unclear")
SINGLE_NO_TARGET = ("supplier", "component", "assembly_only", "adjacent", "buyer_user", "other_role", "unclear")

# positive counter-evidence per family (the only answers that move a row to C)
COUNTER = {"product": frozenset({"component", "assembly_only", "adjacent"}),
           "role": frozenset({"buyer_user", "upstream", "channel", "minority_holder", "other_link"}),
           "target": frozenset({"other_only"})}
POSITIVE = {"product": frozenset({"own"}), "role": frozenset({"supplier"}),
            "target": frozenset({"named", "one_of_several"})}
# single10 label -> (product, role, target) as the tier rule reads it (None: the class says nothing about it)
SINGLE_MAP: dict[str, tuple[str | None, str | None, str | None]] = {
    "supplier": ("own", "supplier", None),
    "supplier_named": ("own", "supplier", "named"),
    "supplier_one_of_several": ("own", "supplier", "one_of_several"),
    "supplier_target_unstated": ("own", "supplier", "not_stated"),
    "supplier_other_target": ("own", "supplier", "other_only"),
    "component": ("component", None, None),
    "assembly_only": ("assembly_only", None, None),
    "adjacent": ("adjacent", None, None),
    "buyer_user": (None, "buyer_user", None),
    "other_role": (None, "other_link", None),
    "unclear": ("none", "unclear", "not_stated"),
}

TIERS = ("A", "B", "C")
TIER_RANK = {"A": 0, "B": 1, "C": 2}
P_DEMOTE = 0.60               # a counter answer moves a row to C only at this mean probability (hysteresis)
BAND = (0.40, 0.75)           # read-0 top probability in this band -> 2 more reads, decided on the mean
BAND_READS = (1, 2)
BAND_SHARE = 0.30             # expected share of items in the band (budget estimate; not a bound)
READ_MAX = 150                # L2-verified rows read (score order); the rest are not_read (tier B)
NOT_READ = "not_read"         # judge_state of a row beyond READ_MAX (the cap, not a budget or service failure)
LAYER_KEY = "_judge_layer"    # set on every ranking entry when the layer applied (order_rank; never written out)
ITEM_USD = 0.00005            # dry-run estimate per item and question (as the facet layer)
REVIEW_MAX = 5                # C rows added to the user's AI review deck (part A)
RULE = ("A = L2 explicit + product own + role supplier + target named or one of several (or no target in the "
        "idea); C = positive counter-evidence at p >= 0.6 (component / only inside an assembly / adjacent product, "
        "buyer or user / upstream / channel / minority holder / other link, other targets only; not stated or "
        "unclear never demotes); B = the rest")

_TAIL = ("Use ONLY state.text and no outside knowledge; the company name alone is not evidence. A consolidated "
         "subsidiary counts as the company itself.")
INSTRUCTIONS = {
    "product": ('Decide what state.issuer itself offers compared with the product or service the investment idea '
                '"{idea}" describes. ' + _TAIL),
    "role": ('Decide which role state.issuer plays for the product or service the investment idea "{idea}" '
             'describes. ' + _TAIL),
    "target": ('Decide whether state.text says that what state.issuer offers for the investment idea "{idea}" '
               'serves the idea\'s target: {target}. ' + _TAIL),
    "single": ('Choose the ONE statement that best describes state.issuer for the investment idea "{idea}"'
               '{target_clause}. ' + _TAIL),
}
CRITERIA = {
    "product": {
        "own": "The company itself offers the idea's product or service as its own product line, segment or "
               "business, sold on its own.",
        "component": "The company offers a component, part or sub-system that goes into the idea's product, not "
                     "the product itself.",
        "assembly_only": "The company makes the idea's product but, as the text says, sells it only built into a "
                         "larger assembly, module or system of its own, never on its own.",
        "adjacent": "The company offers a related but different product or service around the idea (a material, "
                    "tool, production equipment, software or service for it, or a neighbouring product), not the "
                    "product itself.",
        "none": "The text does not show what the company offers for the idea.",
    },
    "role": {
        "supplier": "The company itself makes, supplies or provides it to customers.",
        "buyer_user": "The company buys or uses it as a customer, operator or end user, rather than supplying it.",
        "upstream": "The company supplies raw materials, generic parts or production equipment to the makers of "
                    "it; it does not supply it itself.",
        "channel": "The company only distributes, resells or trades it for others who make it.",
        "minority_holder": "The company only holds a minority stake in, invests in or lends to a company that "
                           "supplies it.",
        "other_link": "The company plays another link of the chain (for example testing, logistics, licensing or "
                      "financing) but does not supply it.",
        "unclear": "The text does not say enough to tell the company's role.",
    },
    "target": {
        "named": "The text names the target ({target}) as the use, market, customer group or place of the "
                 "company's offering.",
        "one_of_several": "The text lists the target ({target}) as one of several uses, markets, customer groups "
                          "or places of the offering.",
        "other_only": "The text names only other uses, markets, customer groups or places for the offering, not "
                      "the target.",
        "not_stated": "The text does not say what the offering is used for, who buys it or where it is sold.",
    },
    "single": {
        "supplier": "The company itself makes, supplies or provides the idea's product or service as its own "
                    "product line, sold on its own.",
        "supplier_named": "The company itself makes, supplies or provides the idea's product or service, sold on "
                          "its own, and the text names the target ({target}) as its use, market or place.",
        "supplier_one_of_several": "The company itself supplies the idea's product or service, and the text lists "
                                   "the target ({target}) as one of several uses, markets or places.",
        "supplier_target_unstated": "The company itself supplies the idea's product or service, but the text does "
                                    "not say what it is used for or where it is sold.",
        "supplier_other_target": "The company itself supplies the idea's kind of product or service, but the text "
                                 "names only other uses, markets or places, not the target.",
        "component": "The company offers a component, part or sub-system that goes into the idea's product, not "
                     "the product itself.",
        "assembly_only": "The company makes the idea's product but sells it only built into a larger assembly, "
                         "module or system of its own, never on its own.",
        "adjacent": "The company offers a related but different product or service around the idea (a material, "
                    "tool, production equipment, software or service for it, or a neighbouring product).",
        "buyer_user": "The company buys or uses the idea's product or service as a customer, operator or end user.",
        "other_role": "The company is upstream of it, a distributor or reseller, a minority holder or another link "
                      "of the chain, and does not supply it itself.",
        "unclear": "The text does not say enough to tell.",
    },
}


# ------------------------------------------------------------------------------------------------ the target

_HEAD_CUT = re.compile(r"\s*(?:\(|,|;|:|\bof\b|\bfor\b|\bin\b|\bwith\b|\bused\b|\bthat\b|\bwhich\b|\bsuch as\b|"
                       r"\bincluding\b)", re.I)
_CONJ = re.compile(r"\s+(?:and|or|&)\s+|\s*/\s*", re.I)


def head_noun(phrase: str | None) -> str | None:
    """The target phrase in head-noun form (deterministic): the words before the first preposition / bracket, the
    last two words of the first conjunct, the last word of the others ('grid-scale battery storage cabinets and
    stations' -> 'storage cabinets / stations'). None when it adds nothing."""
    s = re.sub(r"\s+", " ", str(phrase or "")).strip(" .,;:")
    if not s:
        return None
    core = _HEAD_CUT.split(s, maxsplit=1)[0].strip() or s
    parts = [p.split() for p in _CONJ.split(core) if p.strip()]
    if not parts:
        return None
    heads = [" ".join(parts[0][-2:])] + [p[-1] for p in parts[1:]]
    out = " / ".join(dict.fromkeys(heads))
    return None if out.casefold() == s.casefold() else out


def target_of(cons: Iterable[dict[str, str]] | None) -> dict[str, str] | None:
    """{text, head} of the idea's target from constraints.derive (end market, then place); None when the idea names
    no target (the target question is then not asked and never counts)."""
    by = {c.get("kind"): str(c.get("text") or "").strip() for c in cons or () if isinstance(c, dict)}
    em, geo = by.get("end_market") or "", by.get("geography") or ""
    if not em and not geo:
        return None
    text = f"{em}, in or to {geo}" if em and geo else (em or geo)
    head = head_noun(em) if em else None
    return {"text": text, "head": head} if head else {"text": text}


def _target_words(t: dict[str, str]) -> str:
    return f"{t['text']} (head noun: {t['head']})" if t.get("head") else t["text"]


# ------------------------------------------------------------------------------------------------ questions

def build_questions(idea: str, idea_en: str | None, mode: str, cons: Iterable[dict[str, str]] | None
                    ) -> dict[str, Any]:
    """{family: jev Question} of an arm: atomic3 -> product, role (+ target when the idea names one); single10 ->
    single. ValueError for an unknown mode."""
    if mode not in MODES:
        raise ValueError(f"judge must be one of {', '.join(MODES)} (got {mode!r})")
    from . import screen
    Question, _ = screen._jev_types()
    q_idea = screen.question_idea(idea, idea_en)
    tgt = target_of(cons)
    tw = _target_words(tgt) if tgt else None          # the instructions: the phrase and its head noun
    tt = tgt["text"] if tgt else None                  # the criteria: the phrase
    out: dict[str, Any] = {}
    if mode == "atomic3":
        fams = ["product", "role"] + (["target"] if tgt else [])
        for fam in fams:
            out[fam] = Question(key=KEYS[fam], instructions=INSTRUCTIONS[fam].format(idea=q_idea, target=tw),
                                criteria={v: t.format(target=tt) for v, t in CRITERIA[fam].items()})
        return out
    labels = SINGLE if tgt else SINGLE_NO_TARGET
    out["single"] = Question(key=KEYS["single"], instructions=INSTRUCTIONS["single"].format(
        idea=q_idea, target_clause=f" (the idea's target: {tw})" if tw else ""),
        criteria={v: CRITERIA["single"][v].format(target=tt) for v in labels})
    return out


def question_sha(questions: dict[str, Any]) -> str | None:
    """sha256 (16 hex) of an arm's questions (None when there are none): a different sha means other item keys."""
    if not questions:
        return None
    body = json.dumps({f: {"key": q.key, "instructions": q.instructions, "criteria": q.criteria}
                       for f, q in sorted(questions.items())}, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def questions_dict(questions: dict[str, Any]) -> dict[str, Any]:
    return {f: {"key": q.key, "instructions": q.instructions, "criteria": dict(q.criteria)}
            for f, q in sorted(questions.items())}


# ------------------------------------------------------------------------------------------------ answers

def _p_top(probs: dict[str, Any] | None) -> float:
    vals = [float(v) for v in (probs or {}).values() if isinstance(v, (int, float))]
    return max(vals) if vals else 0.0


def in_band(probs: dict[str, Any] | None) -> bool:
    return BAND[0] <= _p_top(probs) <= BAND[1]


def aggregate(reads: list[dict[str, Any]], labels: Iterable[str]) -> dict[str, Any] | None:
    """{label, p, n, probs} from the ok reads of one item and question: mean per label, label = argmax (ties: the
    earlier label in the question's order)."""
    labels = list(labels)
    ok = [{k: float(v) for k, v in (r.get("probs") or {}).items() if k in labels and isinstance(v, (int, float))}
          for r in reads if r.get("status") == "ok"]
    ok = [p for p in ok if p]
    if not ok:
        return None
    mean = {v: sum(p.get(v, 0.0) for p in ok) / len(ok) for v in labels}
    best = max(labels, key=lambda v: (round(mean[v], 9), -labels.index(v)))
    return {"label": best, "p": round(mean[best], 4), "n": len(ok),
            "probs": {v: round(x, 4) for v, x in mean.items() if x > 0}}


def facts_of(answers: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """{product|role|target: {label, p}} of one row's answers ({family: aggregate}); a single10 answer is mapped
    to the three families (a family its class says nothing about is left out)."""
    out: dict[str, dict[str, Any]] = {}
    for fam, a in (answers or {}).items():
        if not isinstance(a, dict) or not a.get("label"):
            continue
        p = float(a.get("p") or 0.0)
        if fam == "single":
            for f, v in zip(FAMILIES, SINGLE_MAP.get(a["label"], (None, None, None))):
                if v is not None:
                    out[f] = {"label": v, "p": p}
        elif fam in FAMILIES:
            out[fam] = {"label": a["label"], "p": p}
    return out


def tier_of(l2_label: str | None, answers: dict[str, Any] | None, has_target: bool
            ) -> tuple[str, list[str], str]:
    """(tier, reasons, state) of one L2-verified row by the fixed rule (module doc). reasons: 'family:value' of
    the counter-evidence behind a C; state: checked | unchecked (no answer: tier B, never demoted)."""
    f = facts_of(answers)
    if not f:
        return "B", [], "unchecked"
    reasons = [f"{fam}:{f[fam]['label']}" for fam in FAMILIES
               if fam in f and f[fam]["label"] in COUNTER[fam] and f[fam]["p"] >= P_DEMOTE
               and (fam != "target" or has_target)]
    if reasons:
        return "C", reasons, "checked"
    pos = (l2_label == "explicit" and (f.get("product") or {}).get("label") in POSITIVE["product"]
           and (f.get("role") or {}).get("label") in POSITIVE["role"]
           and (not has_target or (f.get("target") or {}).get("label") in POSITIVE["target"]))
    return ("A" if pos else "B"), [], "checked"


def row_fields(l2_label: str | None, answers: dict[str, Any] | None, has_target: bool) -> dict[str, Any]:
    """The judge_* fields of a row (results.json / the ranking entries)."""
    tier, reasons, state = tier_of(l2_label, answers, has_target)
    out: dict[str, Any] = {"judge_tier": tier, "judge_state": state,
                           "judge_labels": {k: v["label"] for k, v in facts_of(answers).items()} or None}
    if tier == "C":
        out["judge_reason"] = reasons
        out["judge_inferred"] = True          # an inference from the text, shown as such; your AI reviews it
    return out


def order_rank(e: dict[str, Any]) -> int:
    """The tier part of the ranking key (screen._order): 0 for every row when the layer is off (no judge_tier and
    no LAYER_KEY), so the order is the old one. A human yes pin uses the human's label (explicit A, partial B), also
    for a row the pin brought in without a tier; the user's AI's applied yes lifts a C row to B."""
    t = e.get("judge_tier")
    if t is None and not e.get(LAYER_KEY):
        return 0
    uv = e.get("user_verdict")
    if uv in ("explicit", "partial"):
        return TIER_RANK["A" if uv == "explicit" else "B"]
    if t is None:
        return TIER_RANK["B"]
    if t == "C" and e.get("agent_verdict") == "yes" and e.get("agent_state") == "applied":
        return TIER_RANK["B"]
    return TIER_RANK.get(t, 1)


def review_keys(ranked: Iterable[dict[str, Any]], limit: int = REVIEW_MAX) -> list[str]:
    """company_key of the C rows the user's AI should review (highest score first, at most `limit`)."""
    cs = [e for e in ranked if e.get("judge_tier") == "C" and e.get("company_key")]
    cs.sort(key=lambda e: (-(e.get("score") or 0.0), -(e.get("market_cap_usd") or 0), e.get("security_id") or ""))
    return [e["company_key"] for e in cs[:limit]]


def labels_of(fam: str, question) -> list[str]:
    return list(question.criteria)


def band_reads(read_offset: int = 0) -> list[int]:
    """The re-read indices of a band item: 1, 2, or with read_offset N the fresh reads N..N+2 (they replace read 0)."""
    from . import screen
    return screen.band_read_indices(len(BAND_READS) + 1, read_offset) if read_offset > 0 else list(BAND_READS)


def band_factor(read_offset: int = 0) -> float:
    """read 0 plus the expected share of band re-reads (an estimate, not a bound)."""
    return 1 + len(band_reads(read_offset)) * BAND_SHARE


def is_lifted(e: dict[str, Any]) -> bool:
    """A C row that a human pin or the user's AI's applied yes moved up (it is no longer ranked as C)."""
    return e.get("judge_tier") == "C" and order_rank(e) < TIER_RANK["C"]


def estimate_items(n_verified: int, n_questions: int, read_offset: int = 0) -> float:
    """Dry-run estimate of the layer (ITEM_USD per item and question x band_factor)."""
    return round(min(READ_MAX, n_verified) * max(1, n_questions) * ITEM_USD * band_factor(read_offset), 6)


def summary(rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    rows = list(rows)
    out = {t: sum(1 for r in rows if r.get("judge_tier") == t) for t in TIERS}
    out["unchecked"] = sum(1 for r in rows if r.get("judge_state") == "unchecked")
    out[NOT_READ] = sum(1 for r in rows if r.get("judge_state") == NOT_READ)
    return out


# ------------------------------------------------------------------------------------------------ the layer

def run_layer(make, items: list, questions: dict[str, Any], remaining: float, clients: dict[str, Any],
              notes: list[str], tell=None, read_offset: int = 0
              ) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """({company_key: {family: {label, p, n, probs, request_id}}}, layer info). Per question: read 0 of every item,
    then reads 1 and 2 (or with read_offset N the fresh reads N..N+2, which replace read 0) of the items whose read-0
    top probability lies in BAND, decided on the mean. Estimate = the cache-aware read-0 estimate x band_factor
    summed over the questions; skipped with a note when it exceeds `remaining`. A re-read pass whose own estimate
    no longer fits `remaining` (with read 0 of the families still to come kept aside) is skipped with a note (the
    family is decided on read 0). Never raises for Jev errors: they end the layer early (partial)."""
    from . import screen
    info: dict[str, Any] = {"items": len(items), "families": sorted(questions), "status": "ok", "skipped": None,
                            "cost_usd": 0.0, "requests": 0, "band_items": 0, "estimate_usd": None,
                            "read_offset": read_offset}
    out: dict[str, dict[str, Any]] = {}
    if not items or not questions:
        return out, info
    try:
        clients["judge"] = client = make("judge", max(0.0, remaining), False)
    except Exception as e:  # noqa: BLE001
        if not screen._is_error(e, "JevUnavailable"):
            raise
        info.update(status="skipped", skipped="jev_unavailable")
        notes.append(f"逐项判断跳过：AI 服务不可用，名单按原来的顺序 / item-by-item check skipped: Jev unavailable "
                     f"({type(e).__name__}); the list keeps the old order")
        return out, info
    factor = band_factor(read_offset)
    est0 = {fam: float((screen._estimate_uncached(client, items, q) or {}).get("est_cost_usd") or 0.0)
            for fam, q in questions.items()}
    worst = sum(est0.values()) * factor
    info["estimate_usd"] = round(worst, 6)
    if worst > remaining + 1e-12:
        info.update(status="skipped", skipped="budget")
        notes.append("逐项判断跳过：预算不够，名单按原来的顺序 / item-by-item check skipped: not enough budget; the "
                     "list keeps the old order")
        return out, info
    spent0, sent0 = screen._client_stats(client)
    stop = None
    fams = sorted(questions, key=lambda f: ("product", "role", "target", "single").index(f))
    for n_fam, fam in enumerate(fams):
        if stop:
            break
        later = sum(est0[f] for f in fams[n_fam + 1:])      # read 0 of the families still to come stays reserved
        q = questions[fam]
        labels = labels_of(fam, q)
        if tell is not None:
            tell("judge", 0, len(items), getattr(client, "spent_usd", None))
        got, stop, err = screen._call_classify(client, items, q)
        reads: dict[str, list[dict]] = {it.item_id: [r] for it, r in zip(items, got)}
        rid = {it.item_id: r.get("request_id") for it, r in zip(items, got)}
        if stop:
            notes.append(f"逐项判断没读完（{stop}） / item-by-item check incomplete ({err})")
        band = [it for it, r in zip(items, got) if r.get("status") == "ok" and in_band(r.get("probs"))]
        info["band_items"] += len(band)
        for r_i in band_reads(read_offset):
            if stop or not band:
                break
            order = sorted(band, key=lambda it: hashlib.sha256(f"{r_i}:{it.item_id}".encode("utf-8")).hexdigest())
            q_r = dataclasses.replace(q, read=r_i)
            need = float((screen._estimate_uncached(client, order, q_r) or {}).get("est_cost_usd") or 0.0)
            if need + later > remaining - (screen._client_stats(client)[0] - spent0) + 1e-12:
                info["band_skipped"] = info.get("band_skipped", 0) + 1
                notes.append("逐项判断的复读跳过：预算不够，按第一次读的结果定 / item-by-item re-reads skipped: not "
                             "enough budget; decided on the first read")
                break
            got2, stop, e2 = screen._call_classify(client, order, q_r)
            for it, r in zip(order, got2):
                reads[it.item_id].append(r)
            if stop:
                notes.append(f"逐项判断的复读没读完（{stop}） / item-by-item re-reads incomplete ({e2})")
        if read_offset > 0:
            for it in band:
                fresh = reads[it.item_id][1:]
                if any(r.get("status") == "ok" for r in fresh):
                    reads[it.item_id] = fresh
        for it in items:
            agg = aggregate(reads.get(it.item_id) or [], labels)
            if agg is not None:
                out.setdefault(it.item_id, {})[fam] = {**agg, "request_id": rid.get(it.item_id)}
    spent1, sent1 = screen._client_stats(client)
    info.update(cost_usd=round(spent1 - spent0, 6), requests=sent1 - sent0, status="partial" if stop else "ok",
                labelled=len(out))
    return out, info


def stored_answers(rows: Iterable[tuple[str, str, str | None, dict | None, str | None]]
                   ) -> dict[str, dict[str, dict[str, Any]]]:
    """{company_key: {family: {label, p}}} from stored screen_results rows (company_key, layer, label, probs,
    status): what calib.load_pool re-applies (p = the stored mean probability of the label)."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for ck, layer, label, probs, status in rows:
        fam = KEY_FAMILY.get(layer)
        if fam is None or status != "ok" or not label:
            continue
        p = (probs or {}).get(label)
        out.setdefault(ck, {})[fam] = {"label": label, "p": float(p) if isinstance(p, (int, float)) else 0.0}
    return out


def questions_for(idea: str, idea_en: str | None, sieve: dict[str, Any] | None, mode: str
                  ) -> tuple[dict[str, Any], bool]:
    """(questions, has_target) of an arm for a screen: the target comes from constraints.derive over the sieve's
    constraints / facets / the English idea, as the constraint lever reads it (screen and rank_only build it the
    same way; its sha guards rank_only)."""
    from . import constraints, scope, screen
    c_en = idea_en or (idea if not screen.needs_translation(idea) else None)
    c_facets = {**sieve["facets"], "type": scope.facet_kind(sieve)} if scope.facets_of(sieve) else None
    cons = constraints.derive(c_en, facets=c_facets, given=(sieve or {}).get("constraints"))
    return build_questions(idea, idea_en, mode, cons), target_of(cons) is not None
