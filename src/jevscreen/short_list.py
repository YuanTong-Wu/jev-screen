"""A final list shorter than SHORT_LIST rows (novice simulation #3): the chat says so plainly and offers the
companies that passed the first read but were not confirmed (the page's to-confirm section) with a free `why` step,
instead of silence. quickstart.result_block stores `short_list`, quickstart.human_text says `text`.

With the sections (the default since the owner decision of 2026-09-29: the main list holds only the confirmed
companies and is never padded to 10) a short main list is normal, not an error: the line says calmly that the list
only takes companies whose own texts state the business, and points at the page's 待核对 / To confirm section with
three example names and the free `why` step. A single padded list (--no-shortlist, or a result written before the
sections) keeps the earlier wording."""
from __future__ import annotations

from typing import Any

TEXT: dict[str, dict[str, str]] = {
    "zh": {
        "short_list": "名单只有 {n} 家，不到 10 家：原文里明确写了这项业务的公司就这么多，我没有拿不确定的公司凑数。"
                      "页面上「{sec}」还有 {u} 家（如 {ex}）：AI 初读觉得相关，但核对时原文没写明，可以当作待确认名单看；"
                      "想知道某家为什么没进，问我就行（免费）。",
        "short_list0": "名单只有 {n} 家，不到 10 家：原文里明确写了这项业务的公司就这么多，我没有拿不确定的公司凑数。"
                       "可以换个说法，或者放宽范围（比如降低市值门槛）再试。",
        "short_list_none": "这次没有公司入选：没有公司的原文明确写了这项业务。可以换个说法，或者放宽范围（比如降低市值门槛）再试。",
        "next_short": "看待确认的 {u} 家：想知道某家为什么没进名单，就说「为什么没有 {ex}」（免费，几秒）",
        # not every company was checked (a partial or running run, or to-confirm rows the check never read)
        "short_open": "名单只有 {n} 家，不到 10 家，但这不一定是全部：{why}，名单可能还会变多。"
                      "页面上「{sec}」有 {u} 家（如 {ex}）可以当作待确认名单看；想知道某家为什么没进，问我就行（免费）。",
        "short_open0": "名单只有 {n} 家，不到 10 家，但这不一定是全部：{why}，名单可能还会变多。",
        "short_none_open": "这次还没有公司入选，但这不一定是结论：{why}，名单可能还会变。",
        # the sections (main list + to confirm)
        "main_short": "确认名单只收初读判为核心、AI 核对时认为原文写明这项业务的公司，不凑满 10 家。",
        "main_short_agent": "另有 {a} 家原本在待核对里，是你的 AI 逐句读原文后确认的（标着「你的 AI 核对」）。",
        "main_short_agent_all": "确认的 {a} 家原本都在待核对里，是你的 AI 逐句读原文后确认的（标着「你的 AI 核对」），不凑满 10 家。",
        "main_short_confirm": "待核对的 {u} 家（如 {ex}）AI 觉得相关，但初读只算相关业务、原文没写明、只有简介，或被移到了后面，"
                              "放在页面「{sec}」部分。",
        "main_none0": "可以换个说法，或者放宽范围（比如降低市值门槛）再试。",
        "main_none_open": "这不一定是结论：{why}，名单可能还会变。",
        "main_open": "名单可能还会变：{why}。",
        "next_confirm": "看待核对的 {u} 家：想知道某家为什么没确认，就说「为什么 {ex} 待核对」（免费，几秒）",
        "why_unchecked": "{k} 家还没核对（预算用完、核对出错或没排上）",
        "why_run": "这次运行还没有全部做完",
        "may_grow": "名单可能还会变",
    },
    "en": {
        "short_list": "The list has only {n} {co}, fewer than 10: that is how many state this business plainly in their "
                      "own texts; I did not pad it with unsure ones. The page's \"{sec}\" section has {u} more (e.g. "
                      "{ex}): the first read found them related, but the check did not find it stated. Treat them as a "
                      "list to confirm; ask me why any of them is not on the list (free).",
        "short_list0": "The list has only {n} {co}, fewer than 10: that is how many state this business plainly in "
                       "their own texts; I did not pad it with unsure ones. Try other wording or a wider scope (for "
                       "example a lower market-cap floor).",
        "short_list_none": "No company made the list: none states this business plainly in its own texts. Try other "
                           "wording or a wider scope (for example a lower market-cap floor).",
        "next_short": "Look at the {u} to confirm: to see why one is not on the list, say \"why not {ex}\" (free, "
                      "seconds)",
        "short_open": "The list has only {n} {co}, fewer than 10, and it may not be complete: {why}, so the list may "
                      "grow. The page's \"{sec}\" section has {u} more (e.g. {ex}) to confirm; ask me why any of them is "
                      "not on the list (free).",
        "short_open0": "The list has only {n} {co}, fewer than 10, and it may not be complete: {why}, so the list may "
                       "grow.",
        "short_none_open": "No company is on the list yet, and that may not be final: {why}, so the list may grow.",
        # the sections (main list + to confirm)
        "main_short": "The confirmed list only takes companies judged central on the first read whose text the AI "
                      "found states this business plainly; it is not padded to 10.",
        "main_short_agent": "Plus {a} first left to confirm, then confirmed by your AI from the text (marked \"checked "
                            "by your AI\").",
        "main_short_agent_all": "All {a} confirmed were first left to confirm, then confirmed by your AI from the text "
                                "(marked \"checked by your AI\"); the list is not padded to 10.",
        "main_short_confirm": "The {u} to confirm (e.g. {ex}) look related to the AI, but the first read did not judge "
                              "it their central business, their texts do not state it plainly, there is only a profile, "
                              "or they were moved down; they are in the page's \"{sec}\" section.",
        "main_none0": "Try other wording or a wider scope (for example a lower market-cap floor).",
        "main_none_open": "That may not be final: {why}, so the list may change.",
        "main_open": "The list may still change: {why}.",
        "next_confirm": "Look at the {u} to confirm: to see why one is not confirmed, say \"why is {ex} to "
                        "confirm\" (free, seconds)",
        "why_unchecked": "{k} were not checked (the budget ran out, the check failed or did not reach them)",
        "why_run": "this run has not fully finished",
        "may_grow": "the list may grow",
    },
}

SHORT_LIST = 10          # a final list shorter than this is said plainly (page.SHORT_LIST: the page says it too)
# to-confirm rows the check never read (page._status_key groups: budget ran out, failed / uncertain, not sent or not
# run): 'that is how many state it' is not known for them
UNCHECKED_KEYS = ("st_skipped_budget", "st_failed", "st_other")


def unchecked_of(data: dict[str, Any] | None) -> int:
    """How many to-confirm rows of the page the check never read (all of them, not only the rows the page lists)."""
    return sum(int(g.get("n") or 0) for g in (data or {}).get("unverified_groups") or []
               if g.get("key") in UNCHECKED_KEYS)


def complete_of(data: dict[str, Any] | None) -> bool:
    """The list is all there is: the run finished (status ok) and every to-confirm row was checked."""
    return (data or {}).get("status") == "ok" and not unchecked_of(data)


def _section_name(lang: str, key: str = "unverified_title") -> str:
    """A page section's title without its count: 'passed the first read, not confirmed' (unverified_title) or the
    to-confirm section (confirm_title)."""
    from . import page
    t = page.STRINGS[lang][key]
    return t.split("（")[0] if lang == "zh" else t.split(" (")[0]


def _names(rows: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    from . import quickstart
    zh = [x.get("name_zh") or x.get("name_tr") or quickstart.plain_name(x.get("name")) or x.get("ticker")
          for x in rows[:3]]
    en = [quickstart.plain_name(x.get("name")) or x.get("ticker") for x in rows[:3]]
    return [x for x in zh if x], [x for x in en if x]


def of(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """A final list under SHORT_LIST rows: its size, the companies that passed the first read but were not confirmed
    (the page's to-confirm section: count, three example names in each language, the first one's ticker for a free
    `why`). None for a list of SHORT_LIST or more.
    With the sections (data['shortlist']): {sections: True, main, to_confirm, unchecked, complete, examples of the
    to-confirm section, why_ticker} for every list (the counts are said at any length)."""
    from . import page, quickstart
    if data is not None and data.get("shortlist"):
        main, confirm = page.main_and_confirm(data)
        zh, en = _names(confirm)
        return {"sections": True, "main": len(main), "to_confirm": len(confirm), "listed": len(main),
                "agent": sum(1 for r in main if r.get("main_via") == "agent"),
                "unchecked": unchecked_of(data), "complete": complete_of(data), "examples_zh": zh,
                "examples_en": en, "why_ticker": confirm[0].get("ticker") if confirm else None}
    rows = (data or {}).get("rows") or []
    if data is None or len(rows) >= SHORT_LIST:
        return None
    unv = (data or {}).get("unverified") or []
    zh = [x.get("name_zh") or x.get("name_tr") or quickstart.plain_name(x.get("name")) or x.get("ticker")
          for x in unv[:3]]
    en = [quickstart.plain_name(x.get("name")) or x.get("ticker") for x in unv[:3]]
    return {"listed": len(rows), "unverified": int((data or {}).get("unverified_total") or len(unv)),
            "unchecked": unchecked_of(data), "complete": complete_of(data),
            "examples_zh": [x for x in zh if x], "examples_en": [x for x in en if x],
            "why_ticker": (unv[0].get("ticker") if unv else None)}


def steps(short: dict[str, Any] | None, run_id: str | None) -> list[dict[str, Any]]:
    """The free next step of a short list: ask why a to-confirm company is not on it."""
    if short and short.get("sections"):
        if not short.get("to_confirm") or not short.get("why_ticker") or int(short.get("main") or 0) >= SHORT_LIST:
            return []
        ex_zh = (short.get("examples_zh") or [short["why_ticker"]])[0]
        ex_en = (short.get("examples_en") or [short["why_ticker"]])[0]
        return [{"text_zh": TEXT["zh"]["next_confirm"].format(u=short["to_confirm"], ex=ex_zh),
                 "text_en": TEXT["en"]["next_confirm"].format(u=short["to_confirm"], ex=ex_en),
                 "command": f"jevscreen why {short['why_ticker']} --run {run_id} --json", "cost_usd": 0.0,
                 "minutes": 1}]
    if not short or not short.get("unverified") or not short.get("why_ticker"):
        return []
    ex_zh = (short.get("examples_zh") or [short["why_ticker"]])[0]
    ex_en = (short.get("examples_en") or [short["why_ticker"]])[0]
    return [{"text_zh": TEXT["zh"]["next_short"].format(u=short["unverified"], ex=ex_zh),
             "text_en": TEXT["en"]["next_short"].format(u=short["unverified"], ex=ex_en),
             "command": f"jevscreen why {short['why_ticker']} --run {run_id} --json", "cost_usd": 0.0,
             "minutes": 1}]


def text(res: dict[str, Any], lang: str, status: str = "done") -> str | None:
    """The chat line of a list under SHORT_LIST rows (None otherwise). It says the list is all there is only when
    the job is done, the run finished (status ok) and the check read every to-confirm company; otherwise it says
    how many were not checked and that the list may grow."""
    T = TEXT[lang]
    short = res.get("short_list") or {}
    if short.get("sections"):
        return _sections_text(short, lang, status)
    n = int(((res.get("summary") or {}).get("listed")) or 0)
    if n >= SHORT_LIST:
        return None
    k = int(short.get("unchecked") or 0)
    complete = status == "done" and (short["complete"] if "complete" in short
                                      else res.get("status", "ok") == "ok" and not k)
    why = T["why_unchecked"].format(k=k) if k else T["why_run"]
    if n == 0:
        return T["short_list_none"] if complete else T["short_none_open"].format(why=why)
    co = "company" if n == 1 else "companies"
    sep = "、" if lang == "zh" else ", "
    if short.get("unverified"):
        ex = sep.join(short.get(f"examples_{lang}") or []) or "?"
        return T["short_list" if complete else "short_open"].format(n=n, co=co, u=short["unverified"], ex=ex,
                                                                    sec=_section_name(lang), why=why)
    return T["short_list0" if complete else "short_open0"].format(n=n, co=co, why=why)


def _sections_text(short: dict[str, Any], lang: str, status: str) -> str | None:
    """The calm line of a main list under SHORT_LIST (None at SHORT_LIST or more: the counts line says it all):
    only confirmed companies, not padded; the to-confirm section with three examples; 'may still change' when the
    run or the check did not finish."""
    T = TEXT[lang]
    m, u = int(short.get("main") or 0), int(short.get("to_confirm") or 0)
    if m >= SHORT_LIST:
        return None
    k = int(short.get("unchecked") or 0)
    complete = status == "done" and bool(short.get("complete", True)) and not k
    sp = "" if lang == "zh" else " "
    if m == 0 and u == 0:          # the done line already says nothing was confirmed and nothing is left
        return T["main_none0"] if complete else T["main_none_open"].format(
            why=T["why_unchecked"].format(k=k) if k else T["why_run"])
    # an empty main list: the done line already says 'no company could be confirmed'
    a = min(int(short.get("agent") or 0), m)
    # the confirmed list's rule, and the rows your AI moved in (they did not meet the first-read rule: say so)
    parts = ([T["main_short_agent_all"].format(a=a)] if a and a == m else
             [T["main_short"]] + ([T["main_short_agent"].format(a=a)] if a else [])) if m else []
    if u:
        sep = "、" if lang == "zh" else ", "
        ex = sep.join(short.get(f"examples_{lang}") or []) or "?"
        parts.append(T["main_short_confirm"].format(u=u, ex=ex, sec=_section_name(lang, "confirm_title")))
    if not complete:
        parts.append(T["main_open"].format(why=T["why_unchecked"].format(k=k) if k else T["why_run"]))
    return sp.join(parts)
