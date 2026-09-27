"""Scope questions (the scope-questions design): the facet layer's families, the split rule, the human's
scope answers and their enforcement.

The facet layer (screen.py, after L2) asks Jev cheap choice questions over the SAME L2 text: which role a company
plays for the idea (supplier / buyer / upstream parts / hardware to operators / holding / target only), and for a
technology or geography idea whether the excerpt names the idea's target. A question goes to the human only when the
listed companies really split on one of these boundaries (find_splits); the answer applies to every company of that
kind (apply_scope), directly (no trial gate), reversibly, and never touches sieve.rules or the L2 question.

Pure helpers: no store access, no network. Facet labels are inferences (推断): every text that shows them says so.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
import unicodedata
from typing import Any, Iterable

FACET_READ_MAX = 150          # L2-verified companies read by the facet layer (score order), plus pins and checks
FACET_BAND = (0.40, 0.75)     # read-0 top non-neutral probability in this band -> 2 more reads, decided on the mean
FACET_BAND_READS = (1, 2)
FACET_P_ASK = 0.70            # a row counts toward a split / question at this mean probability
FACET_P_ENFORCE = 0.60        # a row is removed or demoted by an answer at this mean probability (hysteresis)
FACET_ITEM_USD = 0.00005      # per facet item (L2 is about $0.0000446 per item; the criteria add ~250 tokens/packet)
FACET_BAND_SHARE = 0.30       # worst-case share of items in the band
VERIFIED_SHARE_EST = 0.35     # dry run: share of L2 items that end up verified
SPLIT_MIN_N = 4
SPLIT_MIN_SHARE_V = 0.10
SPLIT_MIN_SHARE_K = 0.20
SCOPE_Q_MAX = 2               # final questions per version
SCOPE_Q_PROVISIONAL = 3       # provisional splits (they feed the held rows of the agent deck)
IMPLIED_NO_MAX = 2
OVERLAP_MAX = 0.5             # a later split whose side V overlaps an earlier one by more than this is dropped
EXAMPLES_V, EXAMPLES_K = 3, 2

ANSWERS = ("yes", "no", "unsure", "skipped")
SOURCES = ("human", "idea_wording")
EFFECTS = ("remove", "demote")
FAMILY_ORDER = ("role", "scope", "geo")
CHIP_ORDER = ("c", "h", "j", "k", "i", "g")

INSTRUCTIONS = {
    "role": ('Using ONLY state.text, decide which role state.issuer plays for the investment idea "{idea}". Do not '
             "use outside knowledge. The company name alone is not evidence."),
    "scope": ('Using ONLY state.text, decide how specifically state.text describes what state.issuer offers for the '
              'investment idea "{idea}". Do not use outside knowledge. The company name alone is not evidence.'),
    "geo": ('Using ONLY state.text, decide where state.issuer offers what the investment idea "{idea}" describes. '
            "Do not use outside knowledge. The company name alone is not evidence."),
}

# family -> the Jev question key, the value that keeps a company, the neutral values, and per value its chip, the
# effect of a 'no' (不要), the criterion with facets ({category}, {target}) and without (None: that value is read
# only with facets of the named type)
FAMILIES: dict[str, dict[str, Any]] = {
    "role": {
        "key": "facet_role", "keep": "supplier", "neutral": ("unclear",),
        "values": {
            "supplier": {"chip": None, "effect": None,
                         "text": "The company itself offers {category} for {target}: it supplies or provides what the "
                                 "idea describes.",
                         "geography": "The company itself offers {category}: it supplies or provides what the idea "
                                      "describes.",
                         "generic": "The company itself offers or supplies what the idea describes."},
            "target_only": {"chip": "c", "effect": "remove", "only": "technology",
                            "text": "The company builds, sells or uses {target} itself but does not offer {category} "
                                    "for it.",
                            "generic": None},
            "buyer": {"chip": "h", "effect": "remove",
                      "text": "The company buys or uses {category} from others rather than supplying it.",
                      "generic": "The company buys or uses what the idea describes from others rather than supplying "
                                 "it."},
            "holding": {"chip": "i", "effect": "remove",
                        "text": "The company only holds a stake in, invests in or lends to companies that do this.",
                        "generic": "The company only holds a stake in, invests in or lends to companies that do "
                                   "this."},
            "upstream": {"chip": "j", "effect": "remove",
                         "text": "The company supplies generic parts, materials, components or production equipment "
                                 "to makers of {category}; its own products are not {category}.",
                         "generic": "The company supplies generic parts, materials, components or production "
                                    "equipment to makers of what the idea describes; its own products are not that "
                                    "thing."},
            "hardware": {"chip": "k", "effect": "remove",
                         "text": "The company makes hardware or devices that it sells to the companies that provide "
                                 "{category}; it does not provide {category} itself.",
                         "generic": "The company makes hardware or devices that it sells to the companies that "
                                    "provide what the idea describes; it does not provide it itself."},
            "unclear": {"chip": None, "effect": None,
                        "text": "The text does not say enough to tell the company's role.",
                        "generic": "The text does not say enough to tell the company's role."},
        }},
    "scope": {
        "key": "facet_scope", "keep": "specific", "neutral": ("unclear",), "only": "technology",
        "values": {
            "specific": {"chip": None, "effect": None, "text": "The text describes {category} applied to {target}.",
                         "generic": None},
            "general_only": {"chip": "g", "effect": "demote",
                             "text": "The text describes {category} in general, with no mention of {target}.",
                             "generic": None},
            "unclear": {"chip": None, "effect": None, "text": "The text does not say enough to tell.",
                        "generic": None},
        }},
    "geo": {
        "key": "facet_geo", "keep": "in_target", "neutral": ("not_stated",), "only": "geography",
        "values": {
            "in_target": {"chip": None, "effect": None,
                          "text": "The text says the company offers {category} in or to {target}.", "generic": None},
            "outside_only": {"chip": "g", "effect": "remove",
                             "text": "The text says the company offers {category}, but only outside {target}.",
                             "generic": None},
            "not_stated": {"chip": None, "effect": None,
                           "text": "The text does not say where the company offers it.", "generic": None},
        }},
}
KEY_FAMILY = {spec["key"]: fam for fam, spec in FAMILIES.items()}


def chip_of(family: str, value: str) -> str | None:
    return ((FAMILIES.get(family) or {}).get("values", {}).get(value) or {}).get("chip")


def effect_of(family: str, value: str) -> str | None:
    return ((FAMILIES.get(family) or {}).get("values", {}).get(value) or {}).get("effect")


def askable_values(family: str) -> list[str]:
    """The values of a family a question (or an idea-wording default) can be about: those with an effect."""
    return [v for v, s in FAMILIES[family]["values"].items() if s.get("effect")]


# ------------------------------------------------------------------------------------------------ facets, shas

def facets_of(sieve: dict[str, Any] | None) -> dict[str, str] | None:
    f = (sieve or {}).get("facets")
    return f if isinstance(f, dict) and f.get("category") and f.get("target") else None


def facet_kind(sieve: dict[str, Any] | None) -> str | None:
    from . import calib
    return calib.facet_type(facets_of(sieve))


def families_for(sieve: dict[str, Any] | None) -> list[str]:
    """role always; scope for a technology idea; geo for a geography idea (customer ideas: role only, v1)."""
    kind = facet_kind(sieve)
    out = ["role"]
    if kind == "technology":
        out.append("scope")
    if kind == "geography":
        out.append("geo")
    return out


def _sha(obj: Any) -> str:
    body = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def facets_sha(sieve: dict[str, Any] | None) -> str:
    """sha of (facets, facets_zh): every scope answer stores it; a change makes old answers inert (asked again)."""
    sv = sieve or {}
    return _sha([sv.get("facets") or None, sv.get("facets_zh") or None])


def criteria(family: str, sieve: dict[str, Any] | None) -> dict[str, str]:
    """{value: criterion} of a family rendered with the sieve's (English) facets, else the generic wording; values
    that exist only with facets of another type are left out."""
    facets = facets_of(sieve)
    kind = facet_kind(sieve)
    out: dict[str, str] = {}
    for v, spec in FAMILIES[family]["values"].items():
        if spec.get("only") and spec["only"] != kind:
            continue
        if facets:
            tpl = spec.get(kind or "") if isinstance(spec.get(kind or ""), str) else spec["text"]
            out[v] = tpl.format(category=facets["category"], target=facets["target"])
        elif spec.get("generic"):
            out[v] = spec["generic"]
    return out


def build_questions(idea: str, idea_en: str | None, sieve: dict[str, Any] | None, *, names: Iterable[str] = (),
                    tickers: Iterable[str] = (), terms: Iterable[str] = ()) -> tuple[dict[str, Any], list[str]]:
    """({family: jev Question}, notes). Each rendered criteria set must pass calib.validate_rule_text (no company
    name / ticker of the universe); a family that fails is skipped with a note, never blocking. The geography target
    is exempt from the name check (a place is not a company)."""
    from . import calib, screen
    Question, _ = screen._jev_types()
    names, tickers, terms = list(names), list(tickers), list(terms)
    facets = facets_of(sieve)
    kind = facet_kind(sieve)
    q_idea = screen.question_idea(idea, idea_en)
    out: dict[str, Any] = {}
    notes: list[str] = []
    for fam in families_for(sieve):
        crit = criteria(fam, sieve)
        if len(crit) < 2:
            continue
        probs: list[str] = []
        for text in crit.values():
            check = text
            if kind == "geography" and facets:
                check = check.replace(facets["target"], " ")
            probs += calib.validate_rule_text(check, names=names, tickers=tickers, terms=terms)
        if probs:
            notes.append(f"范围检查跳过「{fam}」：问题文字{'、'.join(dict.fromkeys(probs))} / scope check {fam} "
                         "skipped: its wording names a company")
            continue
        out[fam] = Question(key=FAMILIES[fam]["key"], instructions=INSTRUCTIONS[fam].format(idea=q_idea),
                            criteria=crit)
    return out, notes


def question_sha(questions: dict[str, Any]) -> str | None:
    """sha of the rendered family questions (params.facet_question_sha): a change means new Jev item keys."""
    if not questions:
        return None
    return _sha({f: {"key": q.key, "instructions": q.instructions, "criteria": q.criteria}
                 for f, q in sorted(questions.items())})


# ------------------------------------------------------------------------------------------------ reads

def top_non_neutral(family: str, probs: dict[str, float] | None) -> float:
    neutral = set(FAMILIES[family]["neutral"])
    return max([float(p) for v, p in (probs or {}).items() if v not in neutral and isinstance(p, (int, float))]
               or [0.0])


def in_band(family: str, probs: dict[str, float] | None) -> bool:
    return FACET_BAND[0] <= top_non_neutral(family, probs) <= FACET_BAND[1]


def clean_probs(family: str, probs: dict[str, Any] | None) -> dict[str, float]:
    """The probabilities of the family's own labels (an unknown label from a model or a fake is dropped)."""
    known = FAMILIES[family]["values"]
    return {v: float(p) for v, p in (probs or {}).items() if v in known and isinstance(p, (int, float))}


def aggregate(family: str, reads: list[dict[str, Any]]) -> dict[str, Any] | None:
    """{label, p, n, probs} from the ok reads of one item: the mean probability per label, label = argmax of the
    mean (ties: the keep label, then the first neutral, then value order), p = its mean."""
    ok = [clean_probs(family, r.get("probs")) for r in reads if r.get("status") == "ok"]
    ok = [p for p in ok if p]
    if not ok:
        return None
    labels = list(FAMILIES[family]["values"])
    mean = {v: sum(p.get(v, 0.0) for p in ok) / len(ok) for v in labels}
    order = [FAMILIES[family]["keep"]] + list(FAMILIES[family]["neutral"]) + labels
    best = max(labels, key=lambda v: (round(mean[v], 9), -order.index(v)))
    return {"label": best, "p": round(mean[best], 4), "n": len(ok),
            "probs": {v: round(x, 4) for v, x in mean.items() if x > 0}}


def estimate_items(n_items: int, families: int) -> float:
    """Dry-run upper bound of the layer (before any verified set exists)."""
    n = min(FACET_READ_MAX, math.ceil(n_items * VERIFIED_SHARE_EST))
    return round(n * families * FACET_ITEM_USD * 1.6, 6)


# ------------------------------------------------------------------------------------------------ answers

def _entries(sieve: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [e for e in (sieve or {}).get("scope_answers") or [] if isinstance(e, dict)]


def entry_for(sieve: dict[str, Any] | None, family: str, value: str, fsha: str | None) -> dict[str, Any] | None:
    for e in _entries(sieve):
        if e.get("family") == family and e.get("value") == value and e.get("facets_sha") == fsha:
            return e
    return None


def enforced(sieve: dict[str, Any] | None, fsha: str | None) -> list[dict[str, Any]]:
    """The answers that change the list: answer 'no' (human or idea wording) with the current facets_sha."""
    return [e for e in _entries(sieve) if e.get("answer") == "no" and e.get("facets_sha") == fsha
            and e.get("family") in FAMILIES and e.get("value") in FAMILIES[e["family"]]["values"]]


def kept_kinds(sieve: dict[str, Any] | None, fsha: str | None) -> set[str]:
    """The chips of the kinds the human said 要 (keep) to: matching agent no's are not applied."""
    out = set()
    for e in _entries(sieve):
        if e.get("answer") == "yes" and e.get("facets_sha") == fsha:
            c = chip_of(e.get("family") or "", e.get("value") or "")
            if c:
                out.add(c)
    return out


def next_sid(sieve: dict[str, Any] | None, review: dict[str, Any] | None = None, prefix: str = "s") -> str:
    """A new sid / cid: 1 + the highest number ever recorded (scope answers, their history, review.json)."""
    nums = [0]
    pat = re.compile(rf"^{prefix}(\d+)$")
    pools: list[Any] = list(_entries(sieve)) + [h for h in (sieve or {}).get("history") or [] if isinstance(h, dict)]
    for e in pools:
        m = pat.match(str(e.get("sid" if prefix == "s" else "cid") or ""))
        if m:
            nums.append(int(m.group(1)))
    for key in ("questions", "provisional", "defaults", "escalations", "sids", "cids"):
        for q in (review or {}).get(key) or []:
            m = pat.match(str((q.get("sid") if prefix == "s" else q.get("cid")) if isinstance(q, dict) else q or ""))
            if m:
                nums.append(int(m.group(1)))
    return f"{prefix}{max(nums) + 1}"


def validate_entries(data: Any) -> list[str]:
    """validate_sieve's check of sieve.scope_answers."""
    if data is None:
        return []
    if not isinstance(data, list):
        return ["scope_answers must be a list"]
    errs = []
    for i, e in enumerate(data):
        if not isinstance(e, dict):
            errs.append(f"scope_answers[{i}] must be an object")
            continue
        fam, val = e.get("family"), e.get("value")
        if fam not in FAMILIES or val not in FAMILIES[fam]["values"]:
            errs.append(f"scope_answers[{i}]: unknown family/value {fam}.{val}")
        if e.get("answer") not in ANSWERS:
            errs.append(f"scope_answers[{i}].answer must be one of {', '.join(ANSWERS)}")
        if e.get("source", "human") not in SOURCES:
            errs.append(f"scope_answers[{i}].source must be one of {', '.join(SOURCES)}")
        if e.get("effect") is not None and e.get("effect") not in EFFECTS:
            errs.append(f"scope_answers[{i}].effect must be one of {', '.join(EFFECTS)}")
        if not isinstance(e.get("sid"), str) or not re.match(r"^s\d+$", e.get("sid") or ""):
            errs.append(f"scope_answers[{i}].sid must look like s1")
    return errs


def _now() -> str:
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def record(sieve: dict[str, Any], entries: list[dict[str, Any]], *, now: str | None = None) -> dict[str, Any]:
    """sieve' with `entries` appended to scope_answers; an earlier entry for the same (family, value) moves to
    sieve.history (so a flip s1=yes after s1=no is just a newer answer). Not saved."""
    now = now or _now()
    sv = json.loads(json.dumps(sieve))
    cur = [e for e in sv.get("scope_answers") or [] if isinstance(e, dict)]
    for new in entries:
        keep = []
        for old in cur:
            if old.get("family") == new["family"] and old.get("value") == new["value"]:
                sv.setdefault("history", []).append({**old, "superseded_at": now, "superseded_by": new["sid"]})
            else:
                keep.append(old)
        cur = keep + [{**new, "answered_at": new.get("answered_at") or now}]
    sv["scope_answers"] = cur
    return sv


def answer_entry(q: dict[str, Any], answer: str, *, fsha: str, run_id: str | None, via: str, raw: str,
                 source: str = "human") -> dict[str, Any]:
    """A scope_answers entry for question `q` (a questions() item, or a default line) answered `answer`."""
    return {"sid": q["sid"], "family": q["family"], "value": q["value"], "answer": answer, "source": source,
            "effect": q.get("effect") or effect_of(q["family"], q["value"]), "facets_sha": fsha,
            "question_sha": q.get("question_sha"), "raw_tokens": raw, "relayed_via": via, "run_id": run_id,
            "examples": list(q.get("examples_v") or [])[:EXAMPLES_V], "rows_changed": q.get("rows_changed")}


# ------------------------------------------------------------------------------------------------ idea wording

def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").casefold()


def implied_defaults(idea: str, implied_no: Any, sieve: dict[str, Any] | None) -> tuple[list[dict[str, Any]],
                                                                                       list[str]]:
    """([{family, value, because}], notes) of the `--facets` implied_no entries that may become defaults: at most
    IMPLIED_NO_MAX; key a value this facet type reads (role.* always, target_only only for technology; geo.* only
    for geography); scope.general_only is refused (a judgement about the evidence, not the idea); `because`, after
    NFKC + casefold, must be words of the idea itself. Others are dropped with a note."""
    out, notes = [], []
    if not isinstance(implied_no, list):
        return out, notes
    kind = facet_kind(sieve)
    fams = set(families_for(sieve))
    for i, e in enumerate(implied_no):
        if len(out) >= IMPLIED_NO_MAX:
            notes.append(f"implied_no: at most {IMPLIED_NO_MAX} entries; the rest were dropped")
            break
        if not isinstance(e, dict):
            notes.append(f"implied_no[{i}] dropped: not an object")
            continue
        key, because = str(e.get("key") or ""), str(e.get("because") or "").strip()
        fam, _, val = key.partition(".")
        if key == "scope.general_only":
            notes.append("implied_no scope.general_only refused: it is about the evidence, not the idea")
            continue
        spec = (FAMILIES.get(fam) or {}).get("values", {}).get(val)
        if fam not in fams or not spec or not spec.get("effect") or (spec.get("only") and spec["only"] != kind):
            notes.append(f"implied_no {key!r} dropped: not a question this idea's facets can ask")
            continue
        if not because or _fold(because) not in _fold(idea):
            notes.append(f"implied_no {key!r} dropped: its 'because' must be words of the idea itself")
            continue
        if any(x["family"] == fam and x["value"] == val for x in out):
            continue
        out.append({"family": fam, "value": val, "because": because})
    return out, notes


def default_entries(defaults: list[dict[str, Any]], sieve: dict[str, Any], *, fsha: str, run_id: str | None
                    ) -> list[dict[str, Any]]:
    """scope_answers entries (answer no, source idea_wording) for the implied defaults that have no entry yet with
    the current facets_sha (an answer of any kind, e.g. the human's undo, is never overwritten)."""
    out = []
    sv = sieve
    for d in defaults:
        if entry_for(sv, d["family"], d["value"], fsha) is not None:
            continue
        sid = next_sid({**sv, "scope_answers": list(sv.get("scope_answers") or []) + out})
        out.append({"sid": sid, "family": d["family"], "value": d["value"], "answer": "no", "source": "idea_wording",
                    "effect": effect_of(d["family"], d["value"]), "facets_sha": fsha, "question_sha": None,
                    "raw_tokens": None, "relayed_via": None, "run_id": run_id, "examples": [], "rows_changed": None,
                    "because": d["because"]})
    return out


# ------------------------------------------------------------------------------------------------ enforcement

def _label(facets_map: dict[str, Any] | None, ck: str, family: str) -> dict[str, Any] | None:
    lab = ((facets_map or {}).get(ck) or {}).get(family)
    return lab if isinstance(lab, dict) and lab.get("label") else None


def _same_sha(a: Any, b: Any) -> bool:
    return not a or not b or a == b


def is_human(e: dict[str, Any]) -> bool:
    return e.get("user_verdict") is not None


def apply_scope(verified: list[dict[str, Any]], facets_map: dict[str, Any] | None, sieve: dict[str, Any] | None,
                agent: dict[str, dict[str, Any]] | None, *, fsha: str | None, human_keys: set[str] | None = None
                ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """(kept, excluded_by_scope, notes) for screen.pin_and_rank's step 2. For each enforced answer (family f,
    value v) and verified row:
    - hit: its facet label for f is v at p >= FACET_P_ENFORCE on the same evidence (general_only: annual-report rows
      only); also a hit when your AI answered no with the value's chip on the same evidence (applied or escalated);
    - exempt: a human pin (a yes brings it back anyway), or your AI said it is not of this kind (in_group false);
    - remove -> excluded (verdict_source 'scope'); demote -> kept with scope_demoted and the gap badge.
    Rows lacking a label for an enforced family get scope_unchecked. Rows are copies; the input is untouched."""
    rules = enforced(sieve, fsha)
    human_keys = human_keys or set()
    if not rules:
        return [dict(e) for e in verified], [], []
    kept, excluded, notes = [], [], []
    also: dict[str, int] = {}
    for e0 in verified:
        e = dict(e0)
        ck = e.get("company_key")
        if ck in human_keys or e.get("security_id") in human_keys:
            kept.append(e)
            continue
        ag = (agent or {}).get(ck) or {}
        ag_ok = bool(ag) and _same_sha(ag.get("evidence_sha"), e.get("evidence_sha"))
        hit = None
        for r in rules:
            fam, val = r["family"], r["value"]
            lab = _label(facets_map, ck, fam)
            if lab is None:
                e["scope_unchecked"] = True
            held_kind = ag.get("held_kind") or (None if ag.get("held_sid") != r.get("sid") else f"{fam}.{val}")
            if ag_ok and ag.get("in_group") is False and held_kind == f"{fam}.{val}":
                e["scope_exempt_agent"] = r.get("sid")
                continue
            by_label = (lab is not None and lab["label"] == val and float(lab.get("p") or 0) >= FACET_P_ENFORCE
                        and _same_sha(lab.get("evidence_sha"), e.get("evidence_sha"))
                        and (val != "general_only" or e.get("l2_evidence") == "annual_report"))
            by_agent = (ag_ok and ag.get("v") == "no" and ag.get("chip") == chip_of(fam, val)
                        and ag.get("state") in ("applied", "escalated"))
            if by_label or by_agent:
                hit = (r, lab, by_label)
                if not by_label:
                    also[r["sid"]] = also.get(r["sid"], 0) + 1
                break
        if hit is None:
            kept.append(e)
            continue
        r, lab, by_label = hit
        info = {"scope_sid": r["sid"], "scope_value": f"{r['family']}.{r['value']}",
                "scope_p": (lab or {}).get("p") if by_label else None, "scope_source": r.get("source", "human"),
                "scope_by": "facet" if by_label else "agent"}
        if (r.get("effect") or effect_of(r["family"], r["value"])) == "demote":
            e.update(info, scope_demoted=True)
            kept.append(e)
        else:
            e.update(info, verdict_source="scope")
            excluded.append(e)
    for sid, n in also.items():
        notes.append(f"另有 {n} 家你的 AI 也判为这一类，一并移出（{sid}） / {n} more your AI judged to be of this kind "
                     f"were removed too ({sid})")
    return kept, excluded, notes


# ------------------------------------------------------------------------------------------------ splits

def find_splits(rows: list[dict[str, Any]], facets_map: dict[str, Any] | None, sieve: dict[str, Any] | None, *,
                top_n: int, cap: int = SCOPE_Q_MAX, n_verified: int | None = None,
                agent: dict[str, dict[str, Any]] | None = None, drop_out_group: bool = False
                ) -> list[dict[str, Any]]:
    """The boundaries the listed companies really split on (pure). rows: the output rows (rank 1..top_n after the
    current enforcement). Side V of (family f, value v): rows labelled v at p >= FACET_P_ASK without a human pin
    (general_only: annual-report rows only; profile rows are neutral); side K: rows labelled f's keep value at
    p >= FACET_P_ASK. A split needs |V| >= max(4, 10% of the list), |K| >= max(4, 20%), and no scope answer of any
    kind for (f, v) with the current facets_sha. Ordered by rows_changed, then role > scope > geo, then chip order;
    a later split overlapping an earlier side V by more than half is dropped; at most `cap`.
    drop_out_group: side-V rows your AI said are not of this kind (in_group false) are left out first."""
    fsha = facets_sha(sieve)
    T = [r for r in rows if r.get("rank") is not None and int(r["rank"]) <= top_n]
    fams = [f for f in families_for(sieve) if any(_label(facets_map, r.get("company_key"), f) for r in T)]
    kind = facet_kind(sieve)
    out = []
    for fam in fams:
        keep = FAMILIES[fam]["keep"]
        K = [r for r in T if (lab := _label(facets_map, r["company_key"], fam)) and lab["label"] == keep
             and float(lab.get("p") or 0) >= FACET_P_ASK]
        for val in askable_values(fam):
            spec = FAMILIES[fam]["values"][val]
            if spec.get("only") and spec["only"] != kind:
                continue
            if entry_for(sieve, fam, val, fsha) is not None:
                continue
            V = []
            for r in T:
                lab = _label(facets_map, r["company_key"], fam)
                if not lab or lab["label"] != val or float(lab.get("p") or 0) < FACET_P_ASK or is_human(r):
                    continue
                if val == "general_only" and r.get("l2_evidence") != "annual_report":
                    continue
                ag = (agent or {}).get(r["company_key"]) or {}
                if drop_out_group and ag.get("in_group") is False and ag.get("held_kind") == f"{fam}.{val}":
                    continue
                V.append(r)
            need_v = max(SPLIT_MIN_N, math.ceil(SPLIT_MIN_SHARE_V * len(T)))
            need_k = max(SPLIT_MIN_N, math.ceil(SPLIT_MIN_SHARE_K * len(T)))
            if len(V) < need_v or len(K) < need_k:
                continue
            changed = len(V)
            if spec["effect"] == "demote" and n_verified is not None:
                changed = min(len(V), max(0, int(n_verified) - top_n))
            out.append({"family": fam, "value": val, "chip": spec["chip"], "effect": spec["effect"],
                        "V": V, "K": K, "rows_changed": changed})
    out.sort(key=lambda s: (-s["rows_changed"], FAMILY_ORDER.index(s["family"]),
                            CHIP_ORDER.index(s["chip"]) if s["chip"] in CHIP_ORDER else 99))
    kept: list[dict[str, Any]] = []
    for s in out:
        vk = {r["company_key"] for r in s["V"]}
        if any(len(vk & {r["company_key"] for r in k["V"]}) > OVERLAP_MAX * len(vk) for k in kept):
            continue
        kept.append(s)
        if len(kept) >= cap:
            break
    return kept


# ------------------------------------------------------------------------------------------------ wording

Q_ZH = {
    "role.target_only": "{pre}有 {v} 家只做或只用{target}本身，不提供{category}（其中 {v10} 家在你看到的前 10），比如 {ex_v}；"
                        "另有 {k} 家真正提供，比如 {ex_k}。",
    "role.buyer": "{pre}有 {v} 家是{category}的买方或使用者，自己不卖（其中 {v10} 家在你看到的前 10），比如 {ex_v}；"
                  "另有 {k} 家是卖的，比如 {ex_k}。",
    "role.upstream": "{pre}有 {v} 家给做{category}的厂商供应通用零件、材料或设备，自己的产品不是它（其中 {v10} 家在你看到的"
                     "前 10），比如 {ex_v}；另有 {k} 家自己提供{category}，比如 {ex_k}。",
    "role.hardware": "{pre}有 {v} 家卖硬件或设备给提供{category}的公司，自己不提供（其中 {v10} 家在你看到的前 10），比如 "
                     "{ex_v}；另有 {k} 家自己提供，比如 {ex_k}。",
    "role.holding": "{pre}有 {v} 家只是持股或投资做这件事的公司，自己不做（其中 {v10} 家在你看到的前 10），比如 {ex_v}；"
                    "另有 {k} 家自己做，比如 {ex_k}。",
    "scope.general_only": "{pre}有 {v} 家的年报摘录只写了{category}，没写{target}（可能做，只是摘录没写；其中 {v10} 家在你"
                          "看到的前 10），比如 {ex_v}；另有 {k} 家的摘录明确写了{target}，比如 {ex_k}。",
    "geo.outside_only": "{pre}有 {v} 家做{category}，但摘录说的业务不在{target}（其中 {v10} 家在你看到的前 10），比如 "
                        "{ex_v}；另有 {k} 家在{target}做，比如 {ex_k}。",
}
Q_EN = {
    "role.target_only": "{pre}{v} of the top {n} ({v10} in the top 10 you saw) only make or use {target} themselves "
                        "and do not offer {category}, e.g. {ex_v}; {k} really offer it, e.g. {ex_k}.",
    "role.buyer": "{pre}{v} of the top {n} ({v10} in the top 10 you saw) buy or use {category} rather than sell it, "
                  "e.g. {ex_v}; {k} sell it, e.g. {ex_k}.",
    "role.upstream": "{pre}{v} of the top {n} ({v10} in the top 10 you saw) supply generic parts, materials or "
                     "equipment to makers of {category}, and their own products are not {category}, e.g. {ex_v}; "
                     "{k} offer {category} themselves, e.g. {ex_k}.",
    "role.hardware": "{pre}{v} of the top {n} ({v10} in the top 10 you saw) sell hardware or devices to the "
                     "companies that provide {category} and do not provide it themselves, e.g. {ex_v}; {k} provide "
                     "it themselves, e.g. {ex_k}.",
    "role.holding": "{pre}{v} of the top {n} ({v10} in the top 10 you saw) only hold stakes in or invest in companies "
                    "that do this and do not do it themselves, e.g. {ex_v}; {k} do it themselves, e.g. {ex_k}.",
    "scope.general_only": "{pre}the annual-report excerpts of {v} of the top {n} ({v10} in the top 10 you saw) mention "
                          "only {category}, not {target} (they may still do it; the excerpt does not say), e.g. "
                          "{ex_v}; the excerpts of {k} clearly mention {target}, e.g. {ex_k}.",
    "geo.outside_only": "{pre}{v} of the top {n} ({v10} in the top 10 you saw) do {category}, but their excerpts place "
                        "the business outside {target}, e.g. {ex_v}; {k} do it in {target}, e.g. {ex_k}.",
}
PRE_ZH = "AI 读摘录后认为，名单前 {n} 家里"
PRE_EN = "Reading the excerpts, the AI thinks "
ASK_ZH = {"remove": "这类公司要不要留在名单里？", "demote": "要不要把它们留在前面？"}
ASK_EN = {"remove": "Keep this kind of company on the list?", "demote": "Keep them near the top?"}
EFFECT_ZH = {"remove": "（不要：这 {v} 家移出，后面的公司往前补；要：名单不变；不确定：按你的 AI 逐家的判断。免费，几秒更新。）",
             "demote": "（不要：它们排到名单后面，标「摘录没提到{target_bare}」，不删除；要：不变；不确定：按你的 AI 逐家的判断。"
                       "免费，几秒更新。）"}
EFFECT_EN = {"remove": "(Drop: these {v} leave and the next companies move up. Keep: nothing changes. Not sure: your "
                       "AI's call on each one. Free; updates in seconds.)",
             "demote": "(Drop: they move to the end of the list, marked \"excerpt does not mention {target_bare}\", "
                       "nothing is removed. Keep: nothing changes. Not sure: your AI's call on each one. Free; "
                       "updates in seconds.)"}
ANSWER_WORDS = {"zh": {"yes": "要", "no": "不要", "unsure": "不确定"}, "en": {"yes": "Keep", "no": "Drop",
                                                                         "unsure": "Not sure"}}
# the kind of company a default removes, for the idea-wording lines and the chat's removal reasons
KIND_ZH = {"role.target_only": "只做或只用{target}本身、不提供{category}", "role.buyer": "是{category}的买方或使用者",
           "role.holding": "只是持股或投资", "role.upstream": "只供应通用零件、材料或设备",
           "role.hardware": "卖硬件或设备给提供{category}的公司", "geo.outside_only": "业务不在{target}",
           "scope.general_only": "摘录只写了大类、没写{target}"}
KIND_EN = {"role.target_only": "only make or use {target} and do not offer {category}",
           "role.buyer": "buy or use {category} rather than sell it", "role.holding": "only hold stakes or invest",
           "role.upstream": "only supply generic parts, materials or equipment",
           "role.hardware": "sell hardware or devices to the companies that provide {category}",
           "geo.outside_only": "do business outside {target}",
           "scope.general_only": "excerpt names only the broad category, not {target}"}
DEFAULT_ZH = "按你的原话「{because}」，AI 读摘录后认为{kind}的 {x} 家已移出（如 {ex}）；要留着就说「{sid} 要」。"
DEFAULT_EN = ("Going by your own words \"{because}\", the AI read the excerpts and removed {x} that {kind} (e.g. "
              "{ex}); to keep them, say \"{sid} keep\".")
GENERIC_ZH = {"category": "这类产品或服务", "target": "想法里的具体对象", "geo_target": "想法里的地区"}
GENERIC_EN = {"category": "what your idea describes", "target": "what your idea names"}


def words(sieve: dict[str, Any] | None, lang: str) -> dict[str, str]:
    """The facet words of a question in one language: zh uses facets_zh only (never the English facets), quoted
    「…」; without them the generic Chinese. en uses the English facets, else the generic English."""
    sv = sieve or {}
    if lang == "zh":
        fz = sv.get("facets_zh") if isinstance(sv.get("facets_zh"), dict) else {}
        cat, tgt = (fz.get("category") or "").strip(), (fz.get("target") or "").strip()
        geo = facet_kind(sv) == "geography"
        return {"category": f"「{cat}」" if cat else GENERIC_ZH["category"],
                "target": f"「{tgt}」" if tgt else GENERIC_ZH["geo_target" if geo else "target"],
                "target_bare": tgt or GENERIC_ZH["geo_target" if geo else "target"]}
    f = facets_of(sv) or {}
    cat, tgt = (f.get("category") or "").strip(), (f.get("target") or "").strip()
    return {"category": cat or GENERIC_EN["category"], "target": tgt or GENERIC_EN["target"],
            "target_bare": tgt or GENERIC_EN["target"]}


def _example_text(r: dict[str, Any], lang: str, names_zh: dict[str, str], agent: dict[str, dict[str, Any]]) -> str:
    from . import quickstart
    sid = r.get("security_id") or ""
    name = quickstart.plain_name(r.get("name")) or sid
    if lang == "zh" and names_zh.get(sid):
        name = names_zh[sid]
    ag = (agent or {}).get(r.get("company_key")) or {}
    short = ag.get(f"short_{lang}")
    if short:
        return f"{name}（{short}）" if lang == "zh" else f"{name} ({short})"
    return name


def _join(items: list[str], lang: str) -> str:
    return "、".join(items) if lang == "zh" else ", ".join(items)


def questions(splits: list[dict[str, Any]], sieve: dict[str, Any] | None, *, n: int,
              names_zh: dict[str, str] | None = None, agent: dict[str, dict[str, Any]] | None = None,
              review: dict[str, Any] | None = None, sids: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """The question dicts of the splits (spec §4.1): sid, family, value, effect, rows_changed, v10, n, side_v /
    side_k, examples_v (3, highest mean p, never a row your AI said is not of this kind) / examples_k (2, best rank),
    question_zh / question_en (always 「AI 读摘录后认为…」: an inference), effect texts, question_sha, answer words and
    the decide tokens. `sids` keeps the sid of a (family.value) asked before in this version."""
    names_zh = names_zh or {}
    agent = agent or {}
    fsha = facets_sha(sieve)
    out = []
    used: dict[str, Any] = {"questions": list((review or {}).get("questions") or [])}
    for s in splits:
        kind = f"{s['family']}.{s['value']}"
        sid = (sids or {}).get(kind) or next_sid(sieve, used)
        used["questions"].append({"sid": sid})
        facets_map_p = {r["company_key"]: r.get("_p") for r in s["V"]}
        ex_v = [r for r in sorted(s["V"], key=lambda r: (-(r.get("_p") or 0.0), r.get("rank") or 10 ** 6))
                if ((agent.get(r["company_key"]) or {}).get("in_group") is not False)][:EXAMPLES_V]
        ex_k = sorted(s["K"], key=lambda r: r.get("rank") or 10 ** 6)[:EXAMPLES_K]
        v10 = sum(1 for r in s["V"] if (r.get("rank") or 10 ** 6) <= 10)
        q: dict[str, Any] = {"sid": sid, "family": s["family"], "value": s["value"], "kind": kind,
                             "effect": s["effect"], "chip": s["chip"], "rows_changed": s["rows_changed"],
                             "v10": v10, "n": n, "facets_sha": fsha,
                             "side_v": [{"security_id": r.get("security_id"), "company_key": r["company_key"],
                                         "name": r.get("name"), "name_zh": names_zh.get(r.get("security_id") or ""),
                                         "rank": r.get("rank"), "p": facets_map_p.get(r["company_key"]),
                                         "short_zh": (agent.get(r["company_key"]) or {}).get("short_zh"),
                                         "short_en": (agent.get(r["company_key"]) or {}).get("short_en"),
                                         "in_group": (agent.get(r["company_key"]) or {}).get("in_group")}
                                        for r in s["V"]],
                             "side_k": [{"security_id": r.get("security_id"), "company_key": r["company_key"],
                                         "name": r.get("name"), "rank": r.get("rank")} for r in s["K"]],
                             "examples_v": [r.get("security_id") for r in ex_v],
                             "examples_k": [r.get("security_id") for r in ex_k]}
        for lang, Q, PRE, ASK, EFF in (("zh", Q_ZH, PRE_ZH, ASK_ZH, EFFECT_ZH), ("en", Q_EN, PRE_EN, ASK_EN,
                                                                                  EFFECT_EN)):
            w = words(sieve, lang)
            text = Q[kind].format(pre=PRE.format(n=n), n=n, v=len(s["V"]), v10=v10, k=len(s["K"]),
                                  ex_v=_join([_example_text(r, lang, names_zh, agent) for r in ex_v], lang),
                                  ex_k=_join([_example_text(r, lang, names_zh, agent) for r in ex_k], lang),
                                  category=w["category"], target=w["target"])
            if lang == "en":
                text = text[0].upper() + text[1:] if text.startswith("reading") else text
            q[f"question_{lang}"] = text + (" " if lang == "en" else "") + ASK[s["effect"]]
            q[f"effect_{lang}"] = EFF[s["effect"]].format(v=len(s["V"]), target_bare=w["target_bare"])
        q["question_sha"] = hashlib.sha256((q["question_zh"] + "\n" + q["question_en"]).encode("utf-8")).hexdigest()[:16]
        q["answer_words"] = ANSWER_WORDS
        q["tokens"] = {"yes": f"{sid}=yes", "no": f"{sid}=no", "unsure": f"{sid}=?"}
        out.append(q)
    return out


def default_lines(entries: list[dict[str, Any]], removed: dict[str, list[dict[str, Any]]], sieve: dict[str, Any] |
                  None, names_zh: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """One line per idea-wording default that removed at least one row (§4.5), with its undo token."""
    from . import quickstart
    names_zh = names_zh or {}
    out = []
    for e in entries:
        if e.get("source") != "idea_wording" or e.get("answer") != "no":
            continue
        rows = removed.get(e["sid"]) or []
        if not rows:
            continue
        kind = f"{e['family']}.{e['value']}"
        d: dict[str, Any] = {"sid": e["sid"], "family": e["family"], "value": e["value"], "kind": kind,
                             "because": e.get("because"), "x": len(rows),
                             "security_ids": [r.get("security_id") for r in rows], "token": f"{e['sid']}=yes"}
        for lang, KIND, LINE in (("zh", KIND_ZH, DEFAULT_ZH), ("en", KIND_EN, DEFAULT_EN)):
            w = words(sieve, lang)
            ex = [(names_zh.get(r.get("security_id") or "") if lang == "zh" else None)
                  or quickstart.plain_name(r.get("name")) or r.get("security_id") for r in rows[:3]]
            d[f"text_{lang}"] = LINE.format(because=e.get("because") or "", kind=KIND[kind].format(**w), x=len(rows),
                                            ex=_join([x for x in ex if x], lang), sid=e["sid"])
        out.append(d)
    return out[:IMPLIED_NO_MAX]


def kind_words(kind: str, sieve: dict[str, Any] | None, lang: str) -> str:
    """The plain words of a scope kind ('role.hardware') for why / the diff / the chat."""
    tpl = (KIND_ZH if lang == "zh" else KIND_EN).get(kind)
    return tpl.format(**words(sieve, lang)) if tpl else kind


def with_p(splits: list[dict[str, Any]], facets_map: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Attach each side-V row's mean facet probability (_p) for the example order."""
    for s in splits:
        for r in s["V"]:
            lab = _label(facets_map, r["company_key"], s["family"]) or {}
            r["_p"] = lab.get("p")
    return splits


def split_brief(s: dict[str, Any]) -> dict[str, Any]:
    """A split without its row dicts (for review.json)."""
    return {"family": s["family"], "value": s["value"], "kind": f"{s['family']}.{s['value']}", "chip": s["chip"],
            "effect": s["effect"], "rows_changed": s["rows_changed"],
            "v": [r["company_key"] for r in s["V"]], "k": [r["company_key"] for r in s["K"]]}


def replace_read(q, r: int):
    return dataclasses.replace(q, read=r)
