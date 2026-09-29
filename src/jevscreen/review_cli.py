"""`jevscreen judge` and `jevscreen decide` (scope design §6.6), and the review flow around a finished run:

- prepare(): at the quickstart's finish step - the provisional scope splits (<= 3) and deck part A for the user's AI
  (review.json + agent_deck_A.json in the run folder);
- judge: records the AI's answers (the agent layer), finalizes the scope questions (<= 2), applies everything with a
  free rank_only version, writes the one page (its scope-question slot) and deck part B (never blocking);
- decide: ONE command with every answer the human gave (sN=yes|no|?, cN=yes|no|?, keep= / drop= / clear=), applied
  directly with a free rank_only version and the diff in plain words.

Nothing here calls Jev: every new version is rank_only (a full from_run under the approval's rest only when the
stored answers do not cover the sieve). Keys are never read or printed.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Callable

EXIT_OK, EXIT_ERROR, EXIT_LOCKED = 0, 1, 3
FOLLOWUP_PREFIX = "F"
RELAYED_TOP = 10
PAGE_ESC_MAX = 3            # the page's question slot shows at most this many companies your AI could not decide


# ------------------------------------------------------------------------------------------------ run access

def run_files(cfg, run_id: str) -> tuple[Path, dict[str, Any]]:
    """(output dir, results.json) of a run (store.StoreLocked / ValueError)."""
    from . import cli, store
    with store.session(cfg, read_only=True, wait_s=10.0) as con:
        _rid, out_dir, res = cli._resolve_run(con, run_id)
    return Path(out_dir), dict(res, output_dir=str(out_dir))


def pool_inputs(cfg, result: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, str]]:
    """(pool, l2 inputs, zh short names) of a run."""
    from . import calib, quickstart, store
    with store.session(cfg, read_only=True, wait_s=10.0) as con:
        pool = calib.load_pool(con, result["run_id"], result.get("params"))
        sids = sorted({p.get("security_id") for p in pool if p.get("security_id")}
                      | {r.get("security_id") for w in ("rows", "unverified", "excluded_by_agent", "excluded_by_scope")
                         for r in result.get(w) or []
                         if r.get("security_id")})
        names = quickstart._local_names(con, sids)
    return pool, calib.load_inputs(result["output_dir"]), names


def stored_filings(cfg, keys) -> dict[str, dict[str, Any]]:
    """company_key -> {text, source_id, form, filing_date, section, doc_id} of the official filing stored for each
    company (screen.load_documents inside a deep view: the deeper business text when a top-N fetch stored one, with
    the same-year summary first; retrieval.doc_text). Read-only; {} when the store is busy or has none: the deck is
    then built without evidence.more (it never blocks the review)."""
    from . import retrieval, screen, store
    from .sources import deep_sections
    keys = [k for k in dict.fromkeys(keys or []) if k]
    if not keys:
        return {}
    try:
        with store.session(cfg, read_only=True, wait_s=10.0) as con, deep_sections.deep_view(keys):
            docs = screen.load_documents(con, company_keys=keys)
    except Exception:          # noqa: BLE001 - the extra text is optional; the review goes on without it
        return {}
    out: dict[str, dict[str, Any]] = {}
    for k, d in docs.items():
        text = retrieval.doc_text(d)
        if text:
            out[k] = {"text": text, **{c: d.get(c) for c in ("source_id", "form", "filing_date", "section",
                                                              "doc_id")}}
    return out


def with_more(cfg, result: dict[str, Any], deck: dict[str, Any]) -> dict[str, Any]:
    """The deck with the stored-filing sentences of its companies (review.add_more)."""
    from . import review
    if not deck.get("items"):
        return deck
    return review.add_more(deck, stored_filings(cfg, [it["company_key"] for it in deck["items"]]), result)


def newest_of(cfg, run_id: str, idea: str) -> str:
    """The newest ok / partial version screened from `run_id` (cli._newest_descendant), else run_id itself."""
    from . import cli, store
    with store.session(cfg, read_only=True, wait_s=10.0) as con:
        return cli._newest_descendant(con, run_id, idea) or run_id


def human_lang(cfg, idea: str) -> str:
    from . import quickstart_cli
    return quickstart_cli.page_lang(cfg, idea)


def load_sieve(cfg, idea: str) -> tuple[dict[str, Any], Path]:
    from . import calib
    path = calib.sieve_path(cfg, idea)
    return calib.load_sieve(path) or calib.new_sieve(idea), path


def max_out_of(result: dict[str, Any]) -> int:
    from . import screen
    return int((result.get("params") or {}).get("max_out") or screen.SCREEN_DEFAULTS["max_out"])


# ------------------------------------------------------------------------------------------------ splits

def _held(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"sid": q["sid"], "kind": q["kind"], "family": q["family"], "value": q["value"],
             "v": [x["company_key"] for x in q.get("side_v") or []]} for q in questions]


def splits_of(result: dict[str, Any], sieve: dict[str, Any], agent: dict[str, Any], *, cap: int,
              drop_out_group: bool = False, exclude: set[str] | None = None, rows: list[dict[str, Any]] | None = None
              ) -> list[dict[str, Any]]:
    from . import scope
    facets = result.get("facets") or {}
    out = scope.find_splits(rows if rows is not None else result.get("rows") or [], facets, sieve,
                            top_n=max_out_of(result), cap=cap + len(exclude or ()),
                            n_verified=(result.get("funnel") or {}).get("l2_verified"), agent=agent,
                            drop_out_group=drop_out_group)
    out = [s for s in out if f"{s['family']}.{s['value']}" not in (exclude or set())][:cap]
    return scope.with_p(out, facets)


def removed_by_default(result: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for r in result.get("excluded_by_scope") or []:
        if r.get("scope_source") == "idea_wording" and r.get("scope_sid"):
            out.setdefault(r["scope_sid"], []).append(r)
    return out


def defaults_of(result: dict[str, Any], sieve: dict[str, Any], names_zh: dict[str, str]) -> list[dict[str, Any]]:
    from . import scope
    fsha = (result.get("params") or {}).get("facets_sha") or scope.facets_sha(sieve)
    return scope.default_lines(scope.enforced(sieve, fsha), removed_by_default(result), sieve, names_zh)


# ------------------------------------------------------------------------------------------------ prepare

def prepare(cfg, result: dict[str, Any], *, lang: str | None = None) -> dict[str, Any]:
    """At the finish step: provisional splits (<= 3), the questions' sids, deck part A for the user's AI, written
    with review.json into the run folder. Returns the review (agent_review.state 'pending', or 'none' when part A
    is empty: the questions are then final at once)."""
    from . import review, scope
    idea = result.get("idea") or ""
    lang = lang or human_lang(cfg, idea)
    sieve, _ = load_sieve(cfg, idea)
    agent = review.agent_verdicts(cfg, idea)
    pool, inputs, names = pool_inputs(cfg, result)
    prov = splits_of(result, sieve, agent, cap=scope.SCOPE_Q_PROVISIONAL)
    old = review.load_review(result["output_dir"])
    qs = scope.questions(prov, sieve, n=max_out_of(result), names_zh=names, agent=agent, review=old)
    deck = with_more(cfg, result, review.build_deck(result, inputs, sieve, agent, part="A", held=_held(qs),
                                                    pool=pool, names_zh=names, human_lang=lang))
    rv: dict[str, Any] = {"run_id": result["run_id"], "idea": idea, "lang": lang, "provisional": qs,
                          "questions": [], "escalations": [], "answered": {},
                          "defaults": defaults_of(result, sieve, names), "relayed": False}
    if deck["items"]:
        path = review.write_deck(deck, result["output_dir"])
        rv = presented(rv, deck)
        rv["agent_review"] = {"state": "pending", "part": "A", "deck_id": deck["deck_id"], "deck_path": str(path),
                              "items": len(deck["items"]), "created_at": review.now_iso(),
                              "record_command": deck["record_command"], "skip_command": deck["skip_command"]}
    else:
        rv["agent_review"] = {"state": "none", "part": "A", "items": 0}
        rv = finalize(cfg, result, rv, sieve=sieve, agent=agent, names=names)
    review.save_review(result["output_dir"], rv)
    return rv


def finalize(cfg, result: dict[str, Any], rv: dict[str, Any], *, sieve: dict[str, Any], agent: dict[str, Any],
             names: dict[str, str], dropped: set[str] | None = None) -> dict[str, Any]:
    """The final questions (<= 2) of a version, keeping the sids of the provisional ones; the escalations; the
    default lines."""
    from . import scope
    sids = {q["kind"]: q["sid"] for q in rv.get("provisional") or []}
    sids.update({q["kind"]: q["sid"] for q in rv.get("questions") or []})
    final = splits_of(result, sieve, agent, cap=scope.SCOPE_Q_MAX, drop_out_group=True, exclude=dropped)
    qs = scope.questions(final, sieve, n=max_out_of(result), names_zh=names, agent=agent, review=rv, sids=sids)
    rv = {**rv, "questions": qs, "defaults": defaults_of(result, sieve, names)}
    rv["escalations"] = escalations_of(cfg, result, sieve, rv)
    return rv


def escalations_of(cfg, result: dict[str, Any], sieve: dict[str, Any], rv: dict[str, Any]) -> list[dict[str, Any]]:
    """The open escalations (verdicts in state 'escalated'), each with a stable cid, ordered: in the relayed top 10
    first, then E3 / E4, then by rank."""
    from . import calib, review, scope
    idea = result.get("idea") or ""
    doc = review.load_agent(cfg, idea)
    inputs = calib.load_inputs(result["output_dir"])
    human = review.human_pins(sieve)
    cids = {e.get("company_key"): e.get("cid") for e in rv.get("escalations") or []}
    out = []
    changed = False
    for v in doc.get("verdicts") or []:
        if v.get("state") != "escalated":
            continue
        cid = v.get("cid") or cids.get(v["company_key"])
        if not cid:
            cid = scope.next_sid(None, {"cids": [x.get("cid") for x in doc.get("verdicts") or []]
                                        + [e["cid"] for e in out]}, prefix="c")
            v["cid"] = cid
            changed = True
        inp = inputs.get(v["company_key"]) or {}
        h = human.get(v["company_key"]) or human.get(v.get("security_id"))
        out.append(review.escalation_item(v, cid, text=inp.get("text"), lang_ev=inp.get("lang"), sieve=sieve,
                                          relayed_top=RELAYED_TOP, human=h))
    if changed:
        with contextlib.suppress(review.AgentStale):
            review.save_agent(cfg, idea, doc)
    out.sort(key=lambda e: (not e["in_relayed_top"], e["reason"] not in ("E3", "E4"), e.get("rank") or 10 ** 6,
                            e["cid"]))
    return out


# ------------------------------------------------------------------------------------------------ page slot

SLOT_ZH = {"yes": "要", "no": "不要", "unsure": "不确定"}
SLOT_EN = {"yes": "Keep", "no": "Drop", "unsure": "Not sure"}


def page_block(rv: dict[str, Any] | None, run_id: str | None, lang: str) -> dict[str, Any]:
    """data['questions'] of the one page: at most 2 scope questions, then at most PAGE_ESC_MAX companies your AI
    could not decide (escalations), plain words in the page's one language, 要 / 不要 / 不确定 buttons whose values
    are decide tokens; the page joins the clicked ones into the line to paste to the AI. Hidden while your AI is
    still checking (state pending)."""
    from . import scope
    rv = rv or {}
    if not rv or (rv.get("agent_review") or {}).get("state") == "pending" or not run_id:
        return {"items": []}
    words = SLOT_ZH if lang == "zh" else SLOT_EN
    ans = rv.get("answered") or {}
    items = []
    for q in (rv.get("questions") or [])[:scope.SCOPE_Q_MAX]:
        if q["sid"] in ans:
            continue
        items.append({"id": q["sid"], "text": q[f"question_{lang}"], "note": q[f"effect_{lang}"],
                      "options": [{"label": words[k], "value": q["tokens"][k]} for k in ("yes", "no", "unsure")]})
    # the companies your AI could not decide (by rank; the chat asks at most CHAT_ESC_MAX of them once): the human
    # answers them here too, with the same tokens, so none is lost when it misses the chat round
    for e in [e for e in rv.get("escalations") or [] if e.get("cid") and e["cid"] not in ans][:PAGE_ESC_MAX]:
        items.append({"id": e["cid"], "text": e[f"question_{lang}"], "note": None, "kind": "company",
                      "options": [{"label": words[k], "value": e["tokens"][k]} for k in ("yes", "no", "unsure")]})
    notes = [d[f"text_{lang}"] for d in rv.get("defaults") or []]
    # each scope default applied from the idea's own words, with its one-click undo line (the page copies it)
    defaults = [{"sid": d.get("sid"), "text": d[f"text_{lang}"],
                 "undo": f'jevscreen decide "{d["token"]}" --run {run_id} --via page'}
                for d in rv.get("defaults") or [] if d.get("token")]
    if not items and not notes:
        return {"items": []}
    return {"template": f'jevscreen decide "{{answers}}" --run {run_id} --via page', "items": items, "notes": notes,
            "defaults": defaults}


def page_questions(cfg, result: dict[str, Any], lang: str) -> dict[str, Any] | None:
    """The slot of a run's page from its review.json (None when the run has none)."""
    from . import review
    rv = review.load_review(result.get("output_dir"))
    if not rv:
        return None
    return page_block(rv, result.get("run_id"), lang)


# ------------------------------------------------------------------------------------------------ versions

class Queued(Exception):
    """The idea's worker holds the quickstart lock (a fill is running): the answers are saved and it re-applies
    them when it ends."""


def new_version(cfg, idea: str, base_run: str, kind: str) -> tuple[dict[str, Any], list[str]]:
    """A new version of `base_run` with the current sieve and agent file: rank_only (free); when the stored answers
    do not cover the sieve, a full from_run under the quickstart approval's rest (its folder and cost are added to
    the approval). Returns (result, notes)."""
    from . import quickstart, review, screen
    notes: list[str] = []
    try:
        return screen.screen(cfg, idea, from_run=base_run, rank_only=True, change_kind=kind, sieve="auto"), notes
    except screen.RankOnlyUnsafe as e:
        reason = e.reason
    job = quickstart.load_job(cfg, quickstart.idea_key(idea))
    budget = 0.0
    if job and job.get("approval"):
        with contextlib.suppress(Exception):
            budget = float(quickstart.remaining_usd(cfg, job, strict=True) or 0.0)
    if budget < 0.001:
        # never spend without the human's approval: the answers are saved; a paid rerun is the human's decision
        cmd = f"jevscreen screen {shlex.quote(idea)} --from-run {base_run} --budget <USD>"
        raise review.AgentError(f"回答已保存，但这次要重新读一部分，需要花钱：请先问你的用户，再运行 {cmd}",
                                f"the answers are saved, but applying them needs a paid re-read (rank_only not "
                                f"possible: {reason}): ask the human first, then run {cmd}")
    out_dir = screen._default_out_dir(cfg, idea, quickstart.store_now())
    res = screen.screen(cfg, idea, from_run=base_run, sieve="auto", budget_usd=max(budget, 0.0), out_dir=out_dir,
                        reads=quickstart.READS)
    if job and job.get("approval"):
        a = job["approval"]
        a.setdefault("out_dirs", []).append(str(out_dir))
        a.setdefault("costs", {})[res["run_id"]] = res.get("cost_usd")
        quickstart.save_job(cfg, job)
    notes.append(f"rank_only not possible ({reason}): screened again from {base_run}")
    # the human was told 'free, seconds': the chat text says what this one cost (paid_text)
    res = {**res, "paid_fallback_usd": float(res.get("cost_usd") or 0.0)}
    return res, notes


def paid_text(res: dict[str, Any], lang: str) -> str:
    """The line the chat adds when applying the answers needed a paid re-read ('' for a free version)."""
    if res.get("paid_fallback_usd") is None:
        return ""
    return TEXT[lang]["paid"].format(c=f"${res['paid_fallback_usd']:.2f}")


def refresh_questions(cfg, idea: str, rv: dict[str, Any], lang: str) -> Path | None:
    """Rewrite only the scope-question slot of the idea's one page (the same version: judge --skip, a timeout)."""
    from . import ops, page, pagestatus
    sp = page.stable_path(cfg, idea)
    with pagestatus.page_lock(cfg, idea):
        data = page.read_page_data(sp) if sp.exists() else None
        if not data or not data.get("run_id"):
            return None
        data["questions"] = page_block(rv, data.get("run_id"), data.get("lang") or lang)
        html = page.render_page(data)
        if ops.scan_secrets(cfg, html):
            return None
        tmp = sp.with_suffix(".rq.tmp")
        tmp.write_text(html, encoding="utf-8")
        tmp.replace(sp)
    return sp


def publish(cfg, res: dict[str, Any], rv: dict[str, Any], *, open_page: bool = False, same: bool = False
            ) -> tuple[Path | None, bool]:
    """Write the version's review.json, cards (for the optional 精调) and page; point the quickstart job at it
    (quickstart.adopt_version). `same`: no new version (only the questions slot of the page changes). Returns
    (page path, job updated)."""
    from . import cli, page, quickstart, review
    review.save_review(res["output_dir"], rv)
    lang = rv.get("lang") or human_lang(cfg, res.get("idea") or "")
    if same:
        deck, data = None, None
        path = refresh_questions(cfg, res.get("idea") or "", rv, lang)
    else:
        deck, _ = cli._write_run_cards(cfg, res, quickstart.CARDS)
        path, data = page.write_page(cfg, res["output_dir"], res, deck, lang=lang)
    from . import calib
    sv = calib.load_sieve(calib.sieve_path(cfg, res.get("idea") or ""))     # None: no sieve file (as reusable() reads)
    adopted = quickstart.adopt_version(cfg, quickstart.idea_key(res["idea"]), res, deck=deck, path=path, data=data,
                                       sieve_version=(sv or {}).get("version"), review=rv, open_page=open_page)
    return path, adopted


def _lock(cfg, idea: str):
    """The quickstart lock when free; Queued when this idea's own worker is running (a fill); otherwise (another
    idea's worker, or none) the commands go on without it (they touch only this idea's files)."""
    from . import guard, quickstart
    try:
        cm = guard.budget_lock(cfg, quickstart.LOCK)
        cm.__enter__()
        return cm
    except guard.Busy:
        job = quickstart.load_job(cfg, quickstart.idea_key(idea))
        age = quickstart._heartbeat_age(job) if job else None
        if job and job.get("state") == "running" and age is not None and age < quickstart.HEARTBEAT_STALE_S:
            raise Queued() from None
        return None


# ------------------------------------------------------------------------------------------------ judge

TEXT = {
    "zh": {"applied": "我核对了 {n} 家的摘录（年报 {a} 家、简介 {b} 家）：移出 {x} 家{names}。",
           "applied0": "我核对了 {n} 家的摘录（年报 {a} 家、简介 {b} 家），名单不用改。",
           "applied_ns": "我核对了 {n} 家的摘录：移出 {x} 家{names}。",
           "applied0_ns": "我核对了 {n} 家的摘录，名单不用改。",
           "applied_answers": "我核对了 {n} 家的摘录（年报 {a} 家、简介 {b} 家）；按你的范围回答和我的核对，共移出 {x} 家{names}。",
           "applied_answers_ns": "我核对了 {n} 家的摘录；按你的范围回答和我的核对，共移出 {x} 家{names}。",
           "queued": "补简介还在进行，完成后会按你的回答自动重排。",
           "skipped": "你的 AI 没有核对，先用系统的名单。", "etc": "等",
           "paid": "这次需要重新读一部分摘录，花了 {c}（在你批准的预算内）。",
           "decided": "已按你的回答调整（免费）。", "nothing": "没有要改的。",
           # the sections: rows your AI moved between the confirmed list and the to-confirm section
           "applied_moved": "我核对了 {n} 家的摘录（年报 {a} 家、简介 {b} 家），没有移出公司。",
           "applied_moved_ns": "我核对了 {n} 家的摘录，没有移出公司。",
           "moved_in": "从原文确认、放进确认名单的 {k} 家{names}。",
           "moved_out": "我认为不符、先移到待核对的 {k} 家{names}（等你定）。"},
    "en": {"applied": "I checked the excerpts of {n} companies ({a} annual reports, {b} profiles): {x} removed{names}.",
           "applied0": "I checked the excerpts of {n} companies ({a} annual reports, {b} profiles); the list stands.",
           "applied_ns": "I checked the excerpts of {n} companies: {x} removed{names}.",
           "applied0_ns": "I checked the excerpts of {n} companies; the list stands.",
           "applied_answers": "I checked the excerpts of {n} companies ({a} annual reports, {b} profiles); with your "
                              "scope answers and my check, {x} removed{names}.",
           "applied_answers_ns": "I checked the excerpts of {n} companies; with your scope answers and my check, "
                                 "{x} removed{names}.",
           "queued": "The profile fill is still running; the list is re-ranked with your answers when it ends.",
           "skipped": "Your AI did not check the excerpts; the system's list stands.", "etc": " and more",
           "paid": "This needed a partial re-read of the excerpts and cost {c} (within the budget you approved).",
           "decided": "Updated with your answers (free).", "nothing": "Nothing to change.",
           "applied_moved": "I checked the excerpts of {n} companies ({a} annual reports, {b} profiles); none "
                            "removed.",
           "applied_moved_ns": "I checked the excerpts of {n} companies; none removed.",
           "moved_in": "Confirmed from the text and moved into the confirmed list: {k}{names}.",
           "moved_out": "Moved to confirm because I judged they do not fit: {k}{names} (your call)."},
}


def _removed_why(r: dict[str, Any], lang: str, sieve: dict[str, Any] | None,
                 words: dict[str, dict[str, str]] | None = None) -> str:
    """The reason words of one removed row: the scope kind, else your AI's chip words (or its why)."""
    from . import review, scope
    if r.get("verdict_source") == "scope":
        return scope.kind_words(r.get("scope_value") or "", sieve, lang)
    words = words if words is not None else review.chip_words(sieve)
    return (words.get(r.get("agent_chip") or "") or {}).get(lang) or r.get(f"agent_why_{lang}") or ""


def _removed_names(rows: list[dict[str, Any]], lang: str, sieve: dict[str, Any] | None, n: int = 5,
                   names: dict[str, str] | None = None) -> str:
    from . import quickstart, review
    words = review.chip_words(sieve)
    parts = []
    for r in rows[:n]:
        zh = r.get("name_zh") or (names or {}).get(r.get("security_id") or "")
        name = (zh if lang == "zh" else None) or quickstart.plain_name(r.get("name")) or r.get("security_id")
        why = _removed_why(r, lang, sieve, words)
        parts.append(f"{name}（{why}）" if lang == "zh" else f"{name} ({why})")
    if not parts:
        return ""
    more = TEXT[lang]["etc"] if len(rows) > n else ""
    return ("——" + "、".join(parts) + more) if lang == "zh" else (": " + ", ".join(parts) + more)


def _removed_rows(res: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """The rows the page lists as removed (page._removed: your AI's no's and every scope removal, the idea-wording
    defaults included), your AI's first; and whether a scope answer of the human removed any (the page's title
    then names both)."""
    scope_rows = list(res.get("excluded_by_scope") or [])
    by_answer = any(r.get("scope_source") != "idea_wording" for r in scope_rows)
    return list(res.get("excluded_by_agent") or []) + scope_rows, by_answer


def _removed_by_agent(res: dict[str, Any]) -> int:
    """How many your AI removed (its no's and the scope kinds it applied): how far part B looks below the cut."""
    return len(res.get("excluded_by_agent") or []) + sum(1 for r in res.get("excluded_by_scope") or []
                                                         if r.get("scope_by") == "agent")


def summary_text(summ: dict[str, Any], lang: str, *, reviewed: int | None = None,
                 removed: int | None = None, by_answers: bool | None = None) -> str:
    """The chat line of your AI's check from its summary; `reviewed` / `removed` / `by_answers` from the version the
    page shows (quickstart's result block) replace the summary's own when they differ, so the chat and the page
    always say the same numbers (the annual-report split is then left out: it belongs to the summary's count)."""
    if summ.get("skipped") or "read" not in summ:
        return summ.get(f"text_{lang}") or TEXT[lang]["skipped"]
    n = int(summ.get("read") or 0) if reviewed is None else int(reviewed)
    x = int(summ.get("removed_total") or 0) if removed is None else int(removed)
    ans = bool(summ.get("by_answers")) if by_answers is None else bool(by_answers)
    if f"names_{lang}" not in summ and summ.get(f"text_{lang}") and n == int(summ.get("read") or 0) \
            and x == int(summ.get("removed_total") or 0):
        return summ[f"text_{lang}"]         # a summary saved before these fields: its own text, same numbers
    a, b = summ.get("annual"), summ.get("profile")
    split = a is not None and b is not None and n == int(summ.get("read") or 0)
    T = TEXT[lang]
    names = summ.get(f"names_{lang}") or ""
    moved = [(k, summ.get(k) or []) for k in ("moved_in", "moved_out") if summ.get(k)]
    if x and ans:
        key = "applied_answers"
    elif x:
        key = "applied"
    else:
        key, names = ("applied_moved" if moved else "applied0"), ""
    if x != int(summ.get("removed_total") or 0):
        names = ""                          # the summary's examples belong to another version's removals
    out = T[key if split else key + "_ns"].format(n=n, a=a, b=b, x=x, names=names)
    for k, rows in moved:
        sep = "、" if lang == "zh" else ", "
        ns = sep.join(str(r.get(f"name_{lang}") or r.get("name_en") or r.get("security_id")) for r in rows[:5])
        ns += T["etc"] if len(rows) > 5 else ""
        out += ("" if lang == "zh" else " ") + T[k].format(k=len(rows), names=("：" + ns) if lang == "zh"
                                                           else f" ({ns})")
    return out


def current_summary(summ: dict[str, Any] | None, res: dict[str, Any] | None) -> dict[str, Any] | None:
    """The agent_summary of the check restated with the counts of the version the page shows (`res`: quickstart's
    result block, agent_reviewed / removed_n / removed_by_answers), so the JSON an agent reads directly says the
    same numbers as the page and the chat. The removed examples and names are dropped when the count changed (they
    belong to another version's removals); the annual-report split when the read count changed."""
    if not summ or summ.get("skipped") or "read" not in summ or not res:
        return summ
    rn, xn, ans = res.get("agent_reviewed"), res.get("removed_n"), res.get("removed_by_answers")
    n = int(summ.get("read") or 0) if rn is None else int(rn)
    x = int(summ.get("removed_total") or 0) if xn is None else int(xn)
    by = bool(summ.get("by_answers")) if ans is None else bool(ans)
    if n == int(summ.get("read") or 0) and x == int(summ.get("removed_total") or 0) \
            and by == bool(summ.get("by_answers")):
        return summ
    out = {**summ, "read": n, "removed_total": x, "by_answers": by}
    if n != int(summ.get("read") or 0):
        out.update(annual=None, profile=None)
    if x != int(summ.get("removed_total") or 0):
        out.update(removed=[], names_zh="", names_en="")
    for lang in ("zh", "en"):
        out[f"text_{lang}"] = summary_text(summ, lang, reviewed=n, removed=x, by_answers=by)
    return out


def agent_summary(res: dict[str, Any], deck: dict[str, Any] | None, answered: int, sieve: dict[str, Any] | None,
                  names: dict[str, str] | None = None, agent: dict[str, dict[str, Any]] | None = None
                  ) -> dict[str, Any]:
    """What your AI's check did, in both languages; `names`: security_id -> the official Chinese short name (the
    Chinese text names the removed companies by it). The counts are the page's: every current verdict of your AI
    (rank_only 'reviewed', all decks, not only this one) and every row the page lists as removed."""
    from . import screen
    items = (deck or {}).get("items") or []
    cur = [v for v in (agent or {}).values() if v.get("state") in screen.STATES_AGENT]
    reviewed = ((res.get("layers") or {}).get("rank_only") or {}).get("reviewed")
    n = int(reviewed) if reviewed is not None else (len(cur) if agent is not None else answered)
    if agent is not None and len(cur) == n and all(v.get("evidence_kind") for v in cur):
        a: int | None = sum(1 for v in cur if v.get("evidence_kind") == "annual_report")
    elif n == answered:
        a = sum(1 for it in items if (it.get("evidence") or {}).get("kind") == "annual_report")
    else:
        a = None                            # verdicts recorded before the kind was kept: no split
    removed, by_answers = _removed_rows(res)
    names = names or {}
    out = {"read": n, "annual": a, "profile": None if a is None else max(0, n - a), "this_deck": answered,
           "run_id": res.get("run_id"), "by_answers": by_answers,
           "removed": [{"security_id": r.get("security_id"), "name": r.get("name"),
                        "name_zh": r.get("name_zh") or names.get(r.get("security_id") or ""),
                        "chip_words_zh": _removed_why(r, "zh", sieve), "chip_words_en": _removed_why(r, "en", sieve)}
                       for r in removed[:5]],
           "removed_total": len(removed)}
    # the sections: the rows your AI moved into the confirmed list (a yes, level explicit, citing the text) and out
    # of it (a no waiting for the human), in the version the page shows
    from . import quickstart
    for key, via in (("moved_in", "agent"), ("moved_out", "agent_no")):
        rows = sorted((r for r in res.get("rows") or [] if r.get("main_via") == via and r.get("rank") is not None),
                      key=lambda r: r["rank"])
        if rows:
            out[key] = [{"security_id": r.get("security_id"),
                         "name_zh": r.get("name_zh") or names.get(r.get("security_id") or "")
                         or quickstart.plain_name(r.get("name")) or r.get("security_id"),
                         "name_en": quickstart.plain_name(r.get("name")) or r.get("security_id")} for r in rows]
    for lang in ("zh", "en"):
        out[f"names_{lang}"] = _removed_names(removed, lang, sieve, names=names)
        out[f"text_{lang}"] = summary_text(out, lang)
    return out


def _deck_of(cfg, deck_id: str) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    from . import review
    run_id, part = review.parse_deck_id(deck_id)
    try:
        out_dir, res = run_files(cfg, run_id)
    except ValueError as e:
        found = _deck_dir_from_jobs(cfg, deck_id)      # a run the store does not list (e.g. replaced by an update)
        if found is None:
            raise review.AgentError(f"找不到核对对应的运行：{e}", f"the deck's run was not found: {e}") from None
        out_dir = found
        try:
            res = dict(json.loads((out_dir / "results.json").read_text(encoding="utf-8")), output_dir=str(out_dir))
        except (OSError, ValueError):
            raise review.AgentError(f"找不到核对对应的运行：{e}", f"the deck's run was not found: {e}") from None
    p = review.deck_path(out_dir, part)
    try:
        deck = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise review.AgentError(f"读不了核对文件 {p}", f"cannot read the deck file {p}") from None
    if deck.get("deck_id") != deck_id:
        raise review.AgentError(f"{p} 不是 {deck_id}", f"{p} is not {deck_id}")
    return deck, out_dir, res


def _deck_dir_from_jobs(cfg, deck_id: str) -> Path | None:
    """The folder of a deck named by a quickstart job (agent_review.deck_path), when the store does not know the
    run by its id."""
    from . import quickstart
    d = quickstart.job_dir(cfg)
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        with contextlib.suppress(Exception):
            job = quickstart.load_job(cfg, p.stem)
            ar = (job or {}).get("agent_review") or {}
            if ar.get("deck_id") == deck_id and ar.get("deck_path"):
                return Path(ar["deck_path"]).parent
    return None


def judge(cfg, deck_id: str, *, file: str | None = None, skip: bool = False) -> tuple[int, dict[str, Any]]:
    """Record the AI's answers to a deck and apply them (a free rank_only version, the page, the quickstart job).
    Returns (exit code, JSON)."""
    from . import calib, review, scope, screen, store
    deck, out_dir, res0 = _deck_of(cfg, deck_id)
    idea = res0.get("idea") or ""
    lang = deck.get("human_lang") or human_lang(cfg, idea)
    part = deck["part"]
    rv0 = review.load_review(out_dir)
    out: dict[str, Any] = {"command": "judge", "deck_id": deck_id, "idea": idea, "applied": False, "queued": False}
    sieve, _ = load_sieve(cfg, idea)
    if skip:
        doc = review.load_agent(cfg, idea)
        answered, raw_sha = 0, None
    else:
        try:
            raw = Path(file).read_bytes()
            data = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, TypeError):
            raise review.AgentError(f"读不了回答文件 {file}", f"cannot read the answers file {file}") from None
        answers = review.parse_answers(deck, data, sieve)
        raw_sha = review.sha_bytes(raw)
        with contextlib.suppress(OSError):
            (out_dir / f"agent_answers_{part}.json").write_bytes(raw)
        answered = len(answers)
        doc = None
        for attempt in (0, 1):
            doc = review.load_agent(cfg, idea)
            doc = _record(cfg, deck, answers, doc, res0, sieve, rv0, raw_sha, data.get("agent"))
            try:
                doc = review.save_agent(cfg, idea, doc)
                break
            except review.AgentStale:
                if attempt:
                    raise
    try:
        cm = _lock(cfg, idea)
    except Queued:
        if part == "A":
            rv0 = {**rv0, "agent_review": {**(rv0.get("agent_review") or {}), "state": "done" if not skip
                                           else "skipped"}}
            review.save_review(out_dir, rv0)
        from . import quickstart
        blocking = part == "A" or (rv0.get("agent_review") or {}).get("deck_id") == deck_id
        if blocking and part != "A":
            rv0 = {**rv0, "agent_review": {**(rv0.get("agent_review") or {}), "state": "done" if not skip
                                           else "skipped"}}
            review.save_review(out_dir, rv0)
        quickstart.write_inbox(cfg, quickstart.idea_key(idea), {"reapply": True, "agent_review_state": (
            "skipped" if skip else "done") if blocking else None})
        out.update(queued=True, text_zh=TEXT["zh"]["queued"], text_en=TEXT["en"]["queued"])
        return EXIT_OK, out
    try:
        base = newest_of(cfg, res0["run_id"], idea)
        _bdir, base_res = run_files(cfg, base) if base != res0["run_id"] else (out_dir, res0)
        rv_base = (review.load_review(_bdir) if base != res0["run_id"] else rv0) or rv0
        agent = review.current(doc)
        dropped: set[str] = set()
        if part == "A" and not skip:
            for q in rv0.get("provisional") or []:
                rev = [v for v in agent.values() if v.get("held_kind") == q["kind"] and v.get("in_group") is not None
                       and v.get("deck_id") == deck_id]
                if rev and sum(1 for v in rev if v["in_group"] is False) > 0.5 * len(rev):
                    dropped.add(q["kind"])
            pool, inputs, _n = pool_inputs(cfg, base_res)
            sim = calib.rerank_result(base_res, sieve, pool=pool, inputs=inputs, agent=agent)
            keep = {f"{s['family']}.{s['value']}" for s in splits_of(base_res, sieve, agent, cap=scope.SCOPE_Q_MAX,
                                                                     drop_out_group=True, exclude=dropped,
                                                                     rows=sim["rows"])}
            for q in rv0.get("provisional") or []:
                if q["kind"] not in keep:
                    doc = review.resolve_held(doc, q["kind"], "unsure", human=review.human_pins(sieve))
            doc = review.save_agent(cfg, idea, doc)
            agent = review.current(doc)
        if skip:
            res, notes = base_res, []               # nothing changes: the system's list stands (no new version)
        else:
            res, notes = new_version(cfg, idea, base, "agent")
        _o, _i, names = pool_inputs(cfg, res)
        prev = {**rv_base, **({"provisional": rv0.get("provisional")} if part == "A" else {})}
        rv = {**prev, "run_id": res["run_id"], "base_run": base, "lang": lang}
        if part == "A":
            rv["agent_review"] = {**(rv0.get("agent_review") or {}), "state": "skipped" if skip else "done",
                                  "answered": answered, "answers_sha": raw_sha, "done_at": review.now_iso()}
            rv = finalize(cfg, res, rv, sieve=sieve, agent=agent, names=names, dropped=dropped)
            left = {q["kind"] for q in rv0.get("provisional") or []} - {q["kind"] for q in rv["questions"]}
            if any(v.get("state") == "held" and v.get("held_kind") in left for v in agent.values()):
                for k in left:
                    doc = review.resolve_held(doc, k, "unsure", human=review.human_pins(sieve))
                doc = review.save_agent(cfg, idea, doc)
                agent = review.current(doc)
                res, more = new_version(cfg, idea, res["run_id"], "agent")
                notes += more
                rv = finalize(cfg, res, {**rv, "run_id": res["run_id"]}, sieve=sieve, agent=agent, names=names,
                              dropped=dropped)
        else:
            rv = {**rv, "defaults": defaults_of(res, sieve, names)}
            rv["escalations"] = escalations_of(cfg, res, sieve, rv)
        summ = agent_summary(res, deck, answered, sieve, names=names, agent=agent) if not skip else {
            "text_zh": TEXT["zh"]["skipped"], "text_en": TEXT["en"]["skipped"], "removed": [], "removed_total": 0,
            "skipped": True}
        rv["agent_summary"] = summ
        part_b = None
        if part == "A":
            pool, inputs, names = pool_inputs(cfg, res)
            deck_b = review.build_deck(res, inputs, sieve, agent, part="B", pool=pool, names_zh=names,
                                       human_lang=lang, removed_so_far=_removed_by_agent(res),
                                       exclude_keys={it["company_key"] for it in deck.get("items") or []})
            deck_b = with_more(cfg, res, deck_b) if not skip else deck_b
            if deck_b["items"] and not skip:
                pb = review.write_deck(deck_b, res["output_dir"])
                rv = presented(rv, deck_b, blocking=False)
                part_b = {"deck_id": deck_b["deck_id"], "deck_path": str(pb), "items": len(deck_b["items"]),
                          "record_command": deck_b["record_command"], "blocking": False, "state": "pending"}
            rv["agent_review"]["part_b"] = part_b
        elif part == "B" and (rv.get("agent_review") or {}).get("part_b"):
            rv["agent_review"] = {**rv["agent_review"], "part_b": {**rv["agent_review"]["part_b"], "state": "done"}}
        elif (rv.get("agent_review") or {}).get("deck_id") == deck_id:
            # the blocking follow-up deck of companies new to the list: checked (or skipped)
            rv["agent_review"] = {**rv["agent_review"], "state": "skipped" if skip else "done",
                                  "answered": answered, "done_at": review.now_iso()}
        if not skip:
            rv["ai_reviews"] = True
            pend_b = (rv.get("agent_review") or {}).get("part_b") or {}
            rv = check_new(cfg, res, rv, sieve=sieve, agent=agent, lang=lang, names=names,
                           base_rows=base_res.get("rows") or [], active=True,
                           exclude=_deck_keys(pend_b.get("deck_path")) if pend_b.get("state") == "pending" else None)
        page_path, adopted = publish(cfg, res, rv, open_page=part == "A", same=skip)
    except store.StoreLocked:
        raise
    finally:
        if cm is not None:
            cm.__exit__(None, None, None)
    out.update(applied=True, run_id=res["run_id"], notes=notes, page=str(page_path) if page_path else None,
               version_of_job=adopted, removed=summ.get("removed") or [], escalated=rv.get("escalations") or [],
               questions=[_q_brief(q) for q in rv.get("questions") or []], defaults=rv.get("defaults") or [],
               part_b=part_b, main_changed=_main_keys(base_res) != _main_keys(res),
               text_zh=" ".join(x for x in (summ["text_zh"], paid_text(res, "zh")) if x),
               text_en=" ".join(x for x in (summ["text_en"], paid_text(res, "en")) if x),
               next_command_en="jevscreen quickstart --status --key "
                               f"{screen_key(idea)} --json (relay text_<lang>, top and ask_now in one message)")
    ar = rv.get("agent_review") or {}
    if ar.get("state") == "pending" and ar.get("deck_id") != deck_id:
        # companies new to the top 10 that your AI has not read: checked before anything is relayed
        out["review_pending"] = {k: ar.get(k) for k in ("deck_id", "deck_path", "items", "record_command",
                                                        "skip_command", "part")}
        out["next_command_en"] = (f"Review the new deck first ({ar.get('items')} companies new to the top 10, "
                                  f"deck_path {ar.get('deck_path')}) the same way and run its record_command; then "
                                  f"jevscreen quickstart --status --key {screen_key(idea)} --json")
    return EXIT_OK, out


def _main_keys(res: dict[str, Any]) -> list[str]:
    """The companies of the list the chat relays, in order: the confirmed list (with the sections) or the first
    RELAYED_TOP rows; judge's main_changed compares them before and after (part B: tell the human only when true)."""
    rows = sorted((r for r in res.get("rows") or [] if r.get("rank") is not None), key=lambda r: r["rank"])
    sections = any(r.get("shortlist_tier") for r in rows)
    return [str(r.get("company_key")) for r in rows if relayed(r, sections)]


def screen_key(idea: str) -> str:
    from . import quickstart
    return quickstart.idea_key(idea)


def _q_brief(q: dict[str, Any]) -> dict[str, Any]:
    return {k: q.get(k) for k in ("sid", "kind", "effect", "rows_changed", "v10", "question_zh", "question_en",
                                  "effect_zh", "effect_en", "examples_v", "examples_k", "tokens", "answer_words")}


def _record(cfg, deck: dict[str, Any], answers: dict[int, dict[str, Any]], doc: dict[str, Any],
            res: dict[str, Any], sieve: dict[str, Any], rv: dict[str, Any], raw_sha: str | None,
            agent_name: Any) -> dict[str, Any]:
    """The agent doc with the answers as verdicts: held for a side-V row of a still-open provisional question, else
    applied / escalated by review.classify."""
    from . import review
    items = {int(it["n"]): it for it in deck.get("items") or []}
    rows = {r["company_key"]: r for w in ("rows", "unverified", "excluded_by_user", "excluded_by_scope",
                                          "excluded_by_agent") for r in res.get(w) or []}
    pool = None
    max_out = max_out_of(res)
    ranked = [r for r in res.get("rows") or [] if r.get("rank") is not None and r["rank"] <= max_out]
    cut = min((r["score"] for r in ranked), default=None) if len(ranked) >= max_out else None
    human = review.human_pins(sieve)
    open_kinds = {q["kind"] for q in rv.get("provisional") or []} if deck.get("part") == "A" else set()
    new = []
    for n, ans in sorted(answers.items()):
        it = items[n]
        row = rows.get(it["company_key"])
        if row is None:
            if pool is None:
                pool, _i, _n = pool_inputs(cfg, res)
            row = next((p for p in pool if p["company_key"] == it["company_key"]), {})
        ctx = review.row_ctx(it, row, max_out=max_out, cut_score=cut)
        h = human.get(it["company_key"]) or human.get(it.get("security_id"))
        if it.get("held_kind") and it["held_kind"] in open_kinds:
            state, code = "held", None
        else:
            state, code = review.classify(ans, ctx, h)
        new.append(review.verdict_of(it, ans, deck=deck, ctx=ctx, state=state, code=code,
                                     answers_sha=raw_sha or "", agent_name=agent_name))
    return review.put_verdicts(doc, new)


# ------------------------------------------------------------------------------------------------ decide

_TOKEN = re.compile(r"^(?:(?P<qid>[sc]\d{1,3})=(?P<ans>yes|no|\?)|(?P<op>keep|drop|clear)=(?P<sid>[^\s,]+))$",
                    re.IGNORECASE)
ANS_MAP = {"yes": "yes", "no": "no", "?": "unsure"}


def parse_tokens(text: str) -> list[dict[str, str]]:
    from . import review
    out = []
    for tok in [t for t in re.split(r"[\s,，]+", text or "") if t]:
        m = _TOKEN.match(tok.strip())
        if not m:
            raise review.AgentError(f"看不懂这个回答：{tok}（写成 s1=no c2=yes keep=代码）",
                                    f"unknown token {tok!r} (write s1=no c2=yes keep=TICKER)")
        if m.group("qid"):
            out.append({"kind": m.group("qid")[0].lower(), "id": m.group("qid").lower(),
                        "answer": ANS_MAP[m.group("ans").lower()], "raw": tok})
        else:
            out.append({"kind": m.group("op").lower(), "id": m.group("sid"), "raw": tok})
    if not out:
        raise review.AgentError("没有回答", "no answers given")
    return out


def _find_security(res: dict[str, Any], pool: list[dict[str, Any]], sid: str) -> dict[str, Any] | None:
    s = sid.strip()
    for w in ("rows", "unverified", "excluded_by_user", "excluded_by_scope", "excluded_by_agent"):
        for r in res.get(w) or []:
            if s in (r.get("security_id"), r.get("company_key")):
                return r
    for p in pool:
        if s in (p.get("security_id"), p.get("company_key")):
            return p
    return None


def decide(cfg, text: str, run: str, *, via: str = "chat") -> tuple[int, dict[str, Any]]:
    """Apply every answer the human gave in ONE call: scope answers (sN), escalations (cN), overrides of the AI's
    calls (keep= / drop=) and clearing them (clear=). Open questions the tokens omit are recorded as skipped.
    Returns (exit code, JSON with the diff in plain words)."""
    from . import calib, review, scope
    toks = parse_tokens(text)
    out_dir, res0 = run_files(cfg, run)
    idea = res0.get("idea") or ""
    lang = human_lang(cfg, idea)
    rv0 = review.load_review(out_dir)
    base = newest_of(cfg, res0["run_id"], idea)
    _bd, base_res = run_files(cfg, base) if base != res0["run_id"] else (out_dir, res0)
    pool, _inputs, names = pool_inputs(cfg, base_res)
    qs = {q["sid"]: q for q in rv0.get("questions") or []}
    defaults = {d["sid"]: d for d in rv0.get("defaults") or []}
    escs = {e["cid"]: e for e in rv0.get("escalations") or []}
    for t in toks:
        if t["kind"] == "s" and t["id"] not in qs and t["id"] not in defaults:
            sv0, _ = load_sieve(cfg, idea)
            if not any(e.get("sid") == t["id"] for e in scope._entries(sv0)):
                raise review.AgentError(f"没有问题 {t['id']}", f"there is no question {t['id']}")
        if t["kind"] == "c" and t["id"] not in escs:
            raise review.AgentError(f"没有要你定的公司 {t['id']}", f"there is no company question {t['id']}")
        if t["kind"] in ("keep", "drop", "clear") and _find_security(base_res, pool, t["id"]) is None:
            raise review.AgentError(f"这次结果里没有 {t['id']}", f"{t['id']} is not in this run")
    raw = " ".join(t["raw"] for t in toks)
    fsha = (base_res.get("params") or {}).get("facets_sha")
    answered: dict[str, str] = dict(rv0.get("answered") or {})
    for attempt in (0, 1):
        sieve, path = load_sieve(cfg, idea)
        fsha = fsha or scope.facets_sha(sieve)
        now = review.now_iso()
        entries, kinds = [], {}
        given = {t["id"]: t for t in toks if t["kind"] == "s"}
        for sid, q in qs.items():
            if sid in answered:
                continue
            t = given.get(sid)
            if t is None and scope.entry_for(sieve, q["family"], q["value"], fsha) is not None:
                continue            # answered meanwhile (a newer version, another tab): never overwritten by a skip
            ans = t["answer"] if t else "skipped"
            entries.append(scope.answer_entry(q, ans, fsha=fsha, run_id=res0["run_id"], via=via, raw=raw))
            kinds[q["kind"]] = ans
        for sid, t in given.items():
            if sid in qs:
                continue
            d = defaults.get(sid) or next((e for e in scope._entries(sieve) if e.get("sid") == sid), None)
            if d:
                entries.append(scope.answer_entry({**d, "sid": sid}, t["answer"], fsha=fsha, run_id=res0["run_id"],
                                                  via=via, raw=raw))
                kinds[f"{d['family']}.{d['value']}"] = t["answer"]
        sv = scope.record(sieve, entries, now=now) if entries else json.loads(json.dumps(sieve))
        doc = review.load_agent(cfg, idea)
        agent = review.current(doc)
        for t in toks:
            if t["kind"] == "c":
                e = escs[t["id"]]
                v = agent.get(e["company_key"]) or {}
                if t["answer"] in ("yes", "no"):
                    want = (v.get("level") or "partial") if t["answer"] == "yes" else "no"
                    sv = _pin(sv, e, want, v.get("chip") if want == "no" else None, "escalation", now,
                              v.get("evidence_sha"), raw)
            elif t["kind"] in ("keep", "drop"):
                r = _find_security(base_res, pool, t["id"]) or {}
                sv = _pin(sv, r, "partial" if t["kind"] == "keep" else "no", None, "override_agent", now,
                          r.get("evidence_sha"), raw)
            elif t["kind"] == "clear":
                r = _find_security(base_res, pool, t["id"]) or {}
                sv = _unpin(sv, r, now)
        try:
            saved = calib.save_sieve(path, sv)
            break
        except calib.SieveStale:
            if attempt:
                raise
    for t in toks:
        if t["kind"] == "c":
            e = escs[t["id"]]
            answered[t["id"]] = t["answer"]
            doc = _set_state(doc, e["company_key"], "not_applied")
    for kind, ans in kinds.items():
        doc = review.resolve_held(doc, kind, ans, human=review.human_pins(saved))
    for sid in qs:
        if sid not in answered:
            answered[sid] = (given.get(sid) or {}).get("answer") or "skipped"
    for t in toks:
        if t["kind"] == "s" and t["id"] not in qs:
            answered[t["id"]] = t["answer"]
    with contextlib.suppress(review.AgentStale):
        doc = review.save_agent(cfg, idea, doc)
    out: dict[str, Any] = {"command": "decide", "idea": idea, "run_id": run, "tokens": raw, "via": via,
                           "applied": False,
                           "queued": False, "recorded": len(entries) + sum(1 for t in toks if t["kind"] != "s")}
    try:
        cm = _lock(cfg, idea)
    except Queued:
        from . import quickstart
        quickstart.write_inbox(cfg, quickstart.idea_key(idea), {"reapply": True})
        out.update(queued=True, text_zh=TEXT["zh"]["queued"], text_en=TEXT["en"]["queued"])
        return EXIT_OK, out
    try:
        res, notes = new_version(cfg, idea, base, "decide")
        _p, _i, names = pool_inputs(cfg, res)
        agent = review.current(doc)
        rv = {**rv0, "run_id": res["run_id"], "base_run": base, "answered": answered, "lang": lang,
              "questions": [q for q in rv0.get("questions") or [] if q["sid"] not in answered],
              "defaults": defaults_of(res, saved, names)}
        rv["escalations"] = [e for e in escalations_of(cfg, res, saved, rv) if e["cid"] not in answered]
        followup = _followup(cfg, res, saved, agent, rv, lang, names,
                             dropped_kinds={k for k, a in kinds.items() if a == "no"})
        if followup:
            rv.setdefault("followups", []).append(followup)
            rv = presented(rv, _deck_of_path(followup.get("deck_path")), blocking=False)
        # companies your answers brought into the list that your AI has not read: checked by it (blocking when one
        # is in the top 10), marked 未核对 until then
        pend_b = (rv.get("agent_review") or {}).get("part_b") or {}
        held = _deck_keys((followup or {}).get("deck_path")) | (
            _deck_keys(pend_b.get("deck_path")) if pend_b.get("state") == "pending" else set())
        rv = check_new(cfg, res, rv, sieve=saved, agent=agent, lang=lang, names=names,
                       base_rows=base_res.get("rows") or [], active=ai_reviews(rv), exclude=held)
        rv["decided"] = (rv.get("decided") or []) + [{"tokens": raw, "via": via, "at": review.now_iso(),
                                                      "run_id": res["run_id"]}]
        page_path, adopted = publish(cfg, res, rv)
    finally:
        if cm is not None:
            cm.__exit__(None, None, None)
    diff = {lg: calib.render_diff(base_res, res, title=TEXT[lg]["decided"], lang=lg, sieve=saved)
            for lg in ("zh", "en")}
    later = [_q_brief(q) for q in rv.get("questions") or []] + rv.get("escalations") or []
    out.update(applied=True, run_id=res["run_id"], notes=notes, page=str(page_path) if page_path else None,
               version_of_job=adopted, diff_zh=diff["zh"], diff_en=diff["en"],
               text_zh="\n".join(x for x in (diff["zh"], paid_text(res, "zh")) if x),
               text_en="\n".join(x for x in (diff["en"], paid_text(res, "en")) if x), followup=followup, later=later)
    ar = rv.get("agent_review") or {}
    if ar.get("state") == "pending" and ar.get("blocking"):
        out["review_pending"] = {k: ar.get(k) for k in ("deck_id", "deck_path", "items", "record_command",
                                                        "skip_command", "part")}
        out["next_command_en"] = (f"Your AI checks the {ar.get('items')} companies new to the top 10 first (deck_path "
                                  f"{ar.get('deck_path')}, the same way as part A; run its record_command), then "
                                  f"jevscreen quickstart --status --key {screen_key(idea)} --json")
    return EXIT_OK, out


def _pin(sv: dict[str, Any], r: dict[str, Any], want: str, chip: str | None, via: str, now: str,
         sha: str | None, raw: str) -> dict[str, Any]:
    """A human pin from decide (source card, pin true, via escalation | override_agent); an earlier card-style
    example of the company moves to history."""
    keep = []
    for ex in sv.get("examples") or []:
        same = isinstance(ex, dict) and ex.get("source", "card") == "card" and (
            (r.get("company_key") and ex.get("company_key") == r.get("company_key"))
            or (r.get("security_id") and ex.get("security_id") == r.get("security_id")))
        if same:
            sv.setdefault("history", []).append({**ex, "superseded_at": now, "superseded_by": via})
        else:
            keep.append(ex)
    sv["examples"] = keep + [{"security_id": r.get("security_id"), "company_key": r.get("company_key"),
                              "name": r.get("name"), "want": want, "chip": chip, "source": "card", "pin": True,
                              "via": via, "at": now, "evidence_sha": sha, "raw_tokens": raw}]
    return sv


def _unpin(sv: dict[str, Any], r: dict[str, Any], now: str) -> dict[str, Any]:
    keep = []
    for ex in sv.get("examples") or []:
        same = isinstance(ex, dict) and ex.get("via") in ("escalation", "override_agent") and (
            ex.get("company_key") == r.get("company_key") or ex.get("security_id") == r.get("security_id"))
        if same:
            sv.setdefault("history", []).append({**ex, "undone_at": now})
        else:
            keep.append(ex)
    sv["examples"] = keep
    return sv


def _set_state(doc: dict[str, Any], ck: str, state: str) -> dict[str, Any]:
    d = json.loads(json.dumps(doc))
    for v in d.get("verdicts") or []:
        if v.get("company_key") == ck:
            v["state"], v["escalation"] = state, None
    return d


def presented(rv: dict[str, Any], deck: dict[str, Any] | None, *, blocking: bool = True) -> dict[str, Any]:
    """rv with the companies of `deck` recorded (company_key -> the evidence sha): a blocking deck (part A, a blocking
    follow-up) in rv['presented'] - your AI had to go through it before the list was relayed, so a company it left
    unanswered is not asked again for the same evidence; a non-blocking one (part B, a follow-up below the top 10) in
    rv['offered'] - not asked again below the top 10, but not counted as checked."""
    field = "presented" if blocking else "offered"
    got = dict(rv.get(field) or {})
    for it in (deck or {}).get("items") or []:
        got[it["company_key"]] = it.get("evidence_sha")
    return {**rv, field: got}


def unchecked_of(res: dict[str, Any], sieve: dict[str, Any] | None, agent: dict[str, Any],
                 inputs: dict[str, Any] | None, base_rows: list[dict[str, Any]] | None,
                 shown: dict[str, Any] | None = None) -> list[str]:
    """The listed companies your AI has not checked (no verdict on the same evidence, never shown to it with this
    evidence, no human decision) that must be: every such company in the relayed top RELAYED_TOP, and every such
    company that entered the list with this version (not listed in `base_rows`, the version before the fill /
    re-rank). In rank order."""
    from . import review
    decided = review._decided(sieve)
    shown = shown or {}
    base = None if base_rows is None else {r.get("company_key") for r in base_rows if r.get("rank") is not None}
    out = []
    sections = any(r.get("shortlist_tier") for r in res.get("rows") or [])
    for r in sorted((r for r in res.get("rows") or [] if r.get("rank") is not None), key=lambda r: r["rank"]):
        ck = r.get("company_key")
        if not ck or ck in decided or r.get("security_id") in decided \
                or r.get("verdict_source") in ("user", "evidence+user"):
            continue
        sha = ((inputs or {}).get(ck) or {}).get("evidence_sha") or r.get("evidence_sha")
        v = agent.get(ck) or {}
        if (v.get("evidence_sha") and v["evidence_sha"] == sha) or (ck in shown and shown[ck] == sha):
            continue
        if relayed(r, sections) or (base is not None and ck not in base):
            out.append(ck)
    return out


def relayed(row: dict[str, Any], sections: bool) -> bool:
    """A row of the list the chat relays: rank <= RELAYED_TOP; with the sections, a row of the confirmed list only
    (its rank is its place there: the main list is numbered first). A to-confirm row is on the page, not in chat."""
    from . import shortlist
    if row.get("rank") is None or int(row["rank"]) > RELAYED_TOP:
        return False
    return not sections or shortlist.in_main(row)


def check_new(cfg, res: dict[str, Any], rv: dict[str, Any], *, sieve: dict[str, Any], agent: dict[str, Any],
              lang: str, names: dict[str, str], base_rows: list[dict[str, Any]] | None,
              active: bool, exclude: set[str] | None = None) -> dict[str, Any]:
    """A new version after a fill / re-rank: rv['unchecked'] = the companies your AI must still check (unchecked_of;
    the page and the chat mark them 未核对 / not yet checked until a verdict exists). When your AI reviews this idea
    (`active`: its part A was answered) they go to it as a BLOCKING follow-up deck F<n> (the same judge flow): rv's
    agent_review becomes that deck, pending, so the list is not relayed before it is checked. `exclude`: companies
    ranked below the top 10 that a pending deck (part B) already holds. Returns rv."""
    from . import review
    pool, inputs, _n = pool_inputs(cfg, res)
    keys = unchecked_of(res, sieve, agent, inputs, base_rows, rv.get("presented"))
    rv = {**rv, "unchecked": keys}
    if not keys or not active:
        return rv
    by_key = {r.get("company_key"): r for r in res.get("rows") or []}
    sections = any(r.get("shortlist_tier") for r in res.get("rows") or [])
    top = [k for k in keys if relayed(by_key.get(k) or {}, sections)]
    offered = rv.get("offered") or {}
    send = top + [k for k in keys if k not in top and k not in (exclude or set()) and k not in offered]
    if not send:
        return rv
    n = 1 + len(rv.get("followups") or [])
    deck = review.build_deck(res, inputs, sieve, agent, part=f"{FOLLOWUP_PREFIX}{n}", pool=pool, names_zh=names,
                             human_lang=lang, followup_keys=send)
    deck = with_more(cfg, res, deck)
    if not deck["items"]:
        return rv
    blocking = any(it["company_key"] in top for it in deck["items"])
    deck["blocking"] = blocking
    p = review.write_deck(deck, res["output_dir"])
    rv = presented(rv, deck, blocking=blocking)
    fu = {"deck_id": deck["deck_id"], "deck_path": str(p), "items": len(deck["items"]), "blocking": blocking,
          "record_command": deck["record_command"], "skip_command": deck["skip_command"]}
    rv["followups"] = list(rv.get("followups") or []) + [fu]
    if not blocking:
        return rv                   # below the top 10 only: your AI checks it after relaying (like part B)
    part_b = (rv.get("agent_review") or {}).get("part_b")
    rv["agent_review"] = {"state": "pending", "part": deck["part"], "deck_id": deck["deck_id"], "deck_path": str(p),
                          "items": len(deck["items"]), "created_at": review.now_iso(), "blocking": True,
                          "record_command": deck["record_command"], "skip_command": deck["skip_command"],
                          "part_b": part_b, "new_companies": len(deck["items"])}
    return rv


def _deck_of_path(path: str | None) -> dict[str, Any] | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")) if path else None
    except (OSError, ValueError):
        return None


def _deck_keys(path: str | None) -> set[str]:
    """The company keys of a written deck ({} when it cannot be read)."""
    if not path:
        return set()
    try:
        return {it["company_key"] for it in json.loads(Path(path).read_text(encoding="utf-8")).get("items") or []}
    except (OSError, ValueError, KeyError, TypeError):
        return set()


def ai_reviews(rv: dict[str, Any] | None) -> bool:
    """Your AI answered this idea's review with a file (part A or later; rv['ai_reviews'], carried to every later
    version), so companies new to the list go to it too. A skipped or timed-out review only marks them."""
    return bool((rv or {}).get("ai_reviews"))


def _followup(cfg, res: dict[str, Any], sieve: dict[str, Any], agent: dict[str, Any], rv: dict[str, Any],
              lang: str, names: dict[str, str], dropped_kinds: set[str] | None = None) -> dict[str, Any] | None:
    """A non-blocking follow-up deck when the change brought companies your AI has not read into the top max_out,
    plus the listed rows labelled 'unclear' in the family of a kind the human just said 不要 to."""
    from . import review
    fams = {k.partition(".")[0] for k in dropped_kinds or ()}
    new = [r["company_key"] for r in res.get("rows") or []
           if not (agent.get(r["company_key"]) or {}).get("evidence_sha") == r.get("evidence_sha")
           and (r.get("backfill") or any(((res.get("facets") or {}).get(r["company_key"]) or {}).get(f, {}).get(
               "label") in ("unclear", "not_stated") for f in fams))]
    if not new:
        return None
    n = 1 + len(rv.get("followups") or [])
    pool, inputs, _n = pool_inputs(cfg, res)
    deck = review.build_deck(res, inputs, sieve, agent, part=f"{FOLLOWUP_PREFIX}{n}", pool=pool, names_zh=names,
                             human_lang=lang, followup_keys=new)
    deck = with_more(cfg, res, deck)
    if not deck["items"]:
        return None
    p = review.write_deck(deck, res["output_dir"])
    return {"deck_id": deck["deck_id"], "deck_path": str(p), "items": len(deck["items"]), "blocking": False,
            "record_command": deck["record_command"]}


# ------------------------------------------------------------------------------------------------ CLI

def add_parsers(sub: Any) -> None:
    j = sub.add_parser("judge", help="record your AI's review of a result (an agent deck from quickstart) and apply "
                       "it: free, seconds")
    j.add_argument("--deck", required=True, metavar="ADECK_ID", help="the deck id (adeck-<run_id>-A|B|F<n>)")
    g = j.add_mutually_exclusive_group(required=True)
    g.add_argument("--file", default=None, metavar="ANSWERS.json", help="the answers (jevscreen.agent_answers/1)")
    g.add_argument("--skip", action="store_true", help="no review: the system's list stands, the questions go on")
    j.add_argument("--json", action="store_true", help="print JSON (for AI agents)")
    d = sub.add_parser("decide", help="apply the human's answers in one call: s1=no s2=yes (scope questions), "
                       "c3=yes (companies your AI asked about), keep=/drop=/clear=TICKER (free, seconds)")
    d.add_argument("tokens", metavar="TOKENS", help='e.g. "s1=no c2=yes keep=SZSE:000887"')
    d.add_argument("--run", required=True, metavar="RUN_ID", help="the run the questions came from")
    d.add_argument("--via", choices=("chat", "page"), default="chat", help="where the human answered")
    d.add_argument("--json", action="store_true", help="print JSON (for AI agents)")


def _emit(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def _run(args: argparse.Namespace, cfg: Any, fn: Callable[[], tuple[int, dict[str, Any]]], name: str) -> int:
    from . import review, store
    try:
        code, out = fn()
    except review.AgentError as e:
        out = {"command": name, "status": "error", "exit_code": EXIT_ERROR, "error_zh": e.text_zh,
               "error_en": e.text_en}
        print(f"error: {name}: {e.text_en}", file=sys.stderr)
        if args.json:
            _emit(out)
        return EXIT_ERROR
    except (ValueError, review.AgentStale) as e:
        out = {"command": name, "status": "error", "exit_code": EXIT_ERROR, "error_en": str(e)[:300]}
        print(f"error: {name}: {str(e)[:300]}", file=sys.stderr)
        if args.json:
            _emit(out)
        return EXIT_ERROR
    except store.StoreLocked as e:
        out = {"command": name, "status": "store_busy", "exit_code": EXIT_LOCKED, "error_en": str(e)[:200]}
        print(f"error: {name}: the database is busy; try again in a moment", file=sys.stderr)
        if args.json:
            _emit(out)
        return EXIT_LOCKED
    out.update(status="queued" if out.get("queued") else "ok", exit_code=code)
    if args.json:
        _emit(out)
    else:
        lang = "zh"
        with contextlib.suppress(Exception):
            from . import quickstart_cli
            lang = quickstart_cli.page_lang(cfg, str(out.get("idea") or ""))
        print(out.get(f"text_{lang}") or out.get("text_en") or "")
    return code


def cmd_judge(args: argparse.Namespace, cfg: Any) -> int:
    return _run(args, cfg, lambda: judge(cfg, args.deck, file=args.file, skip=args.skip), "judge")


def cmd_decide(args: argparse.Namespace, cfg: Any) -> int:
    return _run(args, cfg, lambda: decide(cfg, args.tokens, args.run, via=args.via), "decide")


COMMANDS: dict[str, Callable[[argparse.Namespace, Any], int]] = {"judge": cmd_judge, "decide": cmd_decide}


def decide_command(run_id: str, tokens: str, via: str = "chat") -> str:
    return f"jevscreen decide {shlex.quote(tokens)} --run {run_id} --via {via} --json"
