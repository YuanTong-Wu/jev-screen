"""The confirmed list and the to-confirm section (owner decision 2026-09-29: never pad the list to 10). ON by
default; `screen --no-shortlist` keeps the old single padded list (params['list'] 'padded').

Rows are split into two tiers: `high` (the main list, 确认 / confirmed: L1 judged the company core AND L2 found
explicit evidence, after the `--l2-constraints` check when that is on) and `confirm` (待核对 / to confirm: every other
listed row, a separate, secondary section that never fills the main list). With `--judge` on (rows carry judge_tier), high is tier A of the item-by-item check instead (RULE_JUDGE:
an inference from the text, labelled so on the page). The high tier comes first; ranks are renumbered in that order
and the original rank is kept in `rank_before_shortlist`. Inside the to-confirm tier (confirm_order, structural plan
step 1, free): explicit rows first, then the profile-only rows whose L2 read was explicit (capped at related,
仅简介: measured 84% right against 52% for related + core), then the other related rows; a row moved down (judge
tier C, a scope demotion, a no of your AI it did not apply on its own) stays last. Within each group the list's own
order is kept. A user yes pin listed below the cut (`below_cut`) keeps its real rank and stays at the end.

A row its own page would mark 边缘 / Borderline (`shown_gap`, page.shown_gap: the excerpt shown does not mention
the idea, or the repeated reads were split) is never in the main list: the confirmed list only holds rows whose shown
evidence supports the call. settle() / apply(check=...) mark it on the rows, so the page, quickstart's top, report.md
and the eval all read the same tier; a human yes pin still wins.

Your AI's review and the human move rows between the two (main_of): a to-confirm row your AI judged yes, level
explicit, citing 1-3 sentences (applied) joins the main list after the system's rows, marked `main_via` 'agent'
(你的 AI 核对); a main-list row your AI judged no (escalated or held: an applied no already removed it) goes to the
to-confirm section (`main_via` 'agent_no'); a human yes pin is always in the main list (`main_via` 'user' when the
system alone would not have listed it there), and the human's no / a kind the human keeps win over your AI.

Pure helpers: no store access, no network.
"""
from __future__ import annotations

import copy
from typing import Any, Callable

TIERS = ("high", "confirm")
ORDER = ("; to confirm: explicit first, then profile-only rows capped at related (their read was explicit), then "
         "the other related rows, rows moved down last")
GAP = ("; a row its page marks borderline (the excerpt shown does not mention the idea, or split reads) is to "
       "confirm")
RULE = ("high = L1 core + L2 explicit (after the constraint check when on; unchecked rows are to confirm); "
        "confirm = the others" + GAP + ORDER)
RULE_JUDGE = ("high = tier A of the item-by-item check (inferred: the AI's typed reads of the text say own product, "
              "supplier and the target named; see result['judge']['rule']); confirm = the others" + GAP + ORDER)


def capped_explicit(row: dict[str, Any]) -> bool:
    """A profile-only row whose L2 read was explicit, capped at related (仅简介). Runs before 2026-09-29 do not carry
    l2_label_before_cap: there the mean probabilities tell (explicit above related)."""
    if row.get("l2_label") != "partial" or row.get("l2_evidence") != "profile":
        return False
    if row.get("l2_label_before_cap") is not None:
        return row["l2_label_before_cap"] == "explicit"
    pe, pp = row.get("l2_p_explicit"), row.get("l2_p_partial")
    return isinstance(pe, (int, float)) and isinstance(pp, (int, float)) and pe > pp


def _agent_no(row: dict[str, Any]) -> bool:
    """Your AI said no and it was not set aside by the human (a kind the human keeps: not_applied)."""
    return row.get("agent_verdict") == "no" and row.get("agent_state") != "not_applied"


def _agent_yes_explicit(row: dict[str, Any]) -> bool:
    """Your AI said yes, level explicit, citing 1-3 sentences of the text, applied (not waiting for the human)."""
    ids = row.get("agent_quote_ids")
    return (row.get("agent_verdict") == "yes" and row.get("agent_level") == "explicit"
            and row.get("agent_state") == "applied" and isinstance(ids, list) and 1 <= len(ids) <= 3)


def _human_yes(row: dict[str, Any]) -> bool:
    return row.get("verdict_source") in ("user", "evidence+user") and row.get("user_verdict") in ("explicit",
                                                                                                 "partial")


def main_of(row: dict[str, Any]) -> tuple[str, str | None]:
    """(tier, main_via) of a row after your AI's review and the human's pins: the system's tier (tier_of) unless the
    human said yes (main, 'user'), a scope answer moved it down (scope_demoted: to confirm, whatever your AI said),
    your AI said no to a main-list row (confirm, 'agent_no') or your AI confirmed a
    to-confirm row (main, 'agent'). main_via is None when the system's tier stands."""
    system = tier_of(row)
    if _human_yes(row):
        return "high", (None if system == "high" else "user")
    if row.get("scope_demoted"):
        # the human's scope answer (or the idea's own words) moved it down: never in the main list, and your AI's
        # yes does not undo the human's answer
        return "confirm", None
    if row.get("shown_gap"):
        # its page would say 边缘 / Borderline (a cited sentence of your AI's explicit yes that links the product to
        # the target clears it before this: page.shown_gap)
        return "confirm", None
    if system == "high" and _agent_no(row):
        return "confirm", "agent_no"
    if system == "confirm" and _agent_yes_explicit(row):
        return "high", "agent"
    return system, None


def confirm_order(row: dict[str, Any]) -> tuple[int, int]:
    """Sort key inside the to-confirm tier (stable: the list's order within a group)."""
    moved_down = row.get("judge_tier") == "C" or bool(row.get("scope_demoted")) or _agent_no(row)
    if row.get("l2_label") == "explicit":
        group = 0
    elif capped_explicit(row):
        group = 1
    else:
        group = 2
    return (1 if moved_down else 0, group)


def tier_of(row: dict[str, Any]) -> str:
    """The system's own tier, before scope answers, your AI and the human (main_of applies those).
    high = L1 core + L2 explicit; an explicit row the --l2-constraints check did not reach (`unchecked`: budget,
    Jev unavailable, beyond constraints.READ_MAX) is to confirm, not high confidence; so is a row whose page would
    mark it borderline (shown_gap)."""
    if row.get("shown_gap"):
        return "confirm"
    if row.get("judge_tier") is not None:        # --judge: high = tier A (jevscreen.atomic)
        return "high" if row["judge_tier"] == "A" else "confirm"
    if row.get("l2_constraint") == "unchecked":
        return "confirm"
    return "high" if row.get("l1_label") == "core" and row.get("l2_label") == "explicit" else "confirm"


def gaps(rows: list[dict[str, Any]], check: Callable[[dict[str, Any]], str | None]) -> None:
    """Mark each row's shown_gap ('no_mention' / 'edge', page.shown_gap through `check`; removed when there is
    none). Mutates the rows."""
    for r in rows:
        g = check(r)
        if g:
            r["shown_gap"] = g
        else:
            r.pop("shown_gap", None)


def apply(rows: list[dict[str, Any]], check: Callable[[dict[str, Any]], str | None] | None = None
          ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """(rows with shortlist_tier (and main_via when your AI or the human moved it), the main list first (the system's
    rows, then the ones your AI confirmed) and renumbered, then the to-confirm section; info {high, confirm, rule,
    agent, agent_no, user, gap}). `check` (page.gap_check) marks shown_gap first; without it the rows' own mark
    stands. Mutates the rows."""
    if check is not None:
        gaps(rows, check)
    main = [r for r in rows if not r.get("below_cut")]
    tail = [r for r in rows if r.get("below_cut")]
    for r in rows:
        r["shortlist_tier"], via = main_of(r)
        r.pop("main_via", None)
        if via:
            r["main_via"] = via
    ordered = [r for r in main if r["shortlist_tier"] == "high" and r.get("main_via") != "agent"] + \
        [r for r in main if r["shortlist_tier"] == "high" and r.get("main_via") == "agent"] + \
        sorted((r for r in main if r["shortlist_tier"] != "high"), key=confirm_order)
    for i, r in enumerate(ordered, 1):
        r["rank_before_shortlist"] = r.get("rank")
        r["rank"] = i
    judged = any(r.get("judge_tier") is not None for r in rows)
    # counted over every row, a user pin listed below the cut too (split(), the page and report.md count it the same)
    info = {"high": sum(1 for r in rows if r["shortlist_tier"] == "high"),
            "confirm": sum(1 for r in rows if r["shortlist_tier"] != "high"), "rule": RULE_JUDGE if judged else RULE}
    for via in ("agent", "agent_no", "user"):
        n = sum(1 for r in rows if r.get("main_via") == via)
        if n:
            info[via] = n
    # rows the main rule would list whose page would mark them borderline: in the to-confirm section instead
    n_gap = sum(1 for r in rows if r.get("shown_gap") and r["shortlist_tier"] != "high"
                and _system_high(r))
    if n_gap:
        info["gap"] = n_gap
    if judged:
        info["by"] = "judge"                 # page: the inferred-tier note (page.STRINGS shortlist_note_judge)
    return ordered + tail, info


def on(params: dict[str, Any] | None) -> bool:
    """Whether a new list (a screen, a from_run, a rank_only version) is split into the main list and the
    to-confirm section: always, unless the run was made with --no-shortlist (params['list'] 'padded'). A run made
    before the default changed carries no 'list': a new version of it gets the default. (A saved result renders as
    it was written: the page splits only a result that carries result['shortlist'].)"""
    p = params or {}
    return p.get("list", "main") != "padded"


def in_main(row: dict[str, Any]) -> bool:
    """A row of the main list: its tier when the run wrote one, else the tier it would get (main_of)."""
    t = row.get("shortlist_tier")
    return (t if t in TIERS else main_of(row)[0]) == "high"


def split(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(main list, to-confirm section) of ranked rows, each in rank order (below-cut pins stay at the end)."""
    ranked = [r for r in rows if r.get("rank") is not None]
    return [r for r in ranked if in_main(r)], [r for r in ranked if not in_main(r)]


def _system_high(row: dict[str, Any]) -> bool:
    """The main rule alone (tier_of without the shown_gap mark)."""
    return tier_of({k: v for k, v in row.items() if k != "shown_gap"}) == "high"


def settle(result: dict[str, Any], l2_pieces: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Re-split a result that carries a shortlist with every row's shown_gap marked (page.gap_check over `result`;
    `l2_pieces` default: its run folder's l2_inputs.jsonl): the rows go back to the list's own order
    (rank_before_shortlist) and apply() runs again, so settling twice changes nothing. Mutates and returns
    `result` (its 'rows' list is reordered in place); a result without a shortlist is returned as it is."""
    if not result.get("shortlist"):
        return result
    from . import page
    rows = result.get("rows") or []
    ranked = [r for r in rows if r.get("rank") is not None and not r.get("below_cut")]
    tail = [r for r in rows if r.get("rank") is not None and r.get("below_cut")]
    other = [r for r in rows if r.get("rank") is None]
    for r in ranked:
        if r.get("rank_before_shortlist") is not None:
            r["rank"] = r["rank_before_shortlist"]
    ranked.sort(key=lambda r: r["rank"])
    new_rows, info = apply(ranked + tail, page.gap_check(result, l2_pieces))
    rows[:] = new_rows + other
    result["rows"] = rows
    result["shortlist"] = {**{k: v for k, v in result["shortlist"].items() if k not in ("high", "confirm", "agent",
                                                                                          "agent_no", "user", "gap")},
                           **info}
    return result


def view(result: dict[str, Any]) -> dict[str, Any]:
    """A copy of a saved result as `--shortlist` would have listed it (free: nothing is read again; the rows the
    page would mark borderline are in the to-confirm section, settle). A result that already has a shortlist is
    settled on the copy."""
    out = copy.deepcopy(dict(result))
    if out.get("shortlist"):
        return settle(out)
    rows = sorted((r for r in out.get("rows") or [] if r.get("rank") is not None), key=lambda r: r["rank"])
    from . import page
    out["rows"], out["shortlist"] = apply(rows, page.gap_check(out))
    return out
