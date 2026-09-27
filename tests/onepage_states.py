"""Synthetic quickstart jobs for every state of the one page (tests/test_one_page.py, and screenshots):
fresh, downloading, waiting for the key, key rejected, blocked, screening, done. Invented companies and numbers
only; no network, no real key (a fake key file is written only where a state needs 'a key is configured')."""
from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path
from typing import Any

IDEA_ZH = "储能电站液冷温控系统的核心供应商"
IDEA_EN = "Main suppliers of liquid-cooling thermal management systems for battery energy storage power stations."
FAKE_OR_KEY = "sk-or-v1-" + "fake" * 12
STATES = ("fresh", "downloading", "waiting_key", "key_rejected", "blocked", "screening", "done")
T0 = dt.datetime(2026, 9, 27, 3, 0, 0)


def iso(t: dt.datetime) -> str:
    return t.replace(microsecond=0).isoformat() + "Z"


def base_job(idea: str = IDEA_ZH, lang: str = "zh") -> dict[str, Any]:
    from jevscreen import quickstart as qs
    job = qs.new_job(idea, lang=lang, min_mcap=1e9, countries=None)
    job["created_at"] = iso(T0)
    return job


def step(sid: str, status: str = "ok", **kw: Any) -> dict[str, Any]:
    return {"id": sid, "status": status, "seconds": 1.0, "detail_zh": "", "detail_en": "", **kw}


def job_for(state: str, cfg, *, idea: str = IDEA_ZH, lang: str = "zh", now: dt.datetime | None = None
            ) -> dict[str, Any]:
    """A job in `state`, plus the files the state implies (consent, a fake key, a cooldown marker)."""
    from jevscreen import consent, guard, keys, quickstart as qs
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)
    job = base_job(idea, lang)
    if state == "fresh":
        return job
    consent.record(cfg, "gray-sources", "yes", lang=lang)
    hb = iso(now)
    job["worker"] = {"pid": 1, "started_at": iso(now - dt.timedelta(minutes=2)), "heartbeat_at": hb}
    if state == "downloading":
        job.update(state="running", current_step="descriptions", idea_en=IDEA_EN, idea_en_source="agent")
        job["steps"] = [step("check"), step("universe", companies=3120)]
        job["progress"] = {"phase": 2, "heartbeat_at": hb, "stage": "descriptions",
                           "stage_started_at": iso(now - dt.timedelta(seconds=20)),
                           "stages": {"descriptions": {"started_at": iso(now - dt.timedelta(seconds=20)), "done0": 0,
                                                       "done": 6_300_000, "total": 15_200_000}}}
        return job
    job.update(idea_en=IDEA_EN, idea_en_source="agent")
    job["steps"] = [step("check"), step("universe", companies=3120),
                    step("descriptions", described=2154, share=0.69, mb=15.2), step("pack", "skipped")]
    if state == "waiting_key":
        job.update(state="waiting", waiting_on="key_jev")
        job["progress"] = {"phase": 2, "heartbeat_at": hb}
        return job
    keys.write_key(cfg, "openrouter", FAKE_OR_KEY)
    fp = qs.key_state(cfg)["fingerprint"]
    job["approval"] = {"usd": 1.0, "idea_en_sha": qs.sha12(IDEA_EN), "approved_at": iso(now), "runs": 0,
                       "canary_usd": 0.00005}
    if state == "key_rejected":
        job.update(state="failed", failure={"kind": "ai_unavailable", "error": "HTTP 401", "http_status": 401,
                                            "provider": "openrouter", "at": hb})
        job["canary"] = {"at": hb, "status": 401, "cost_usd": 0.0, "fingerprint": fp}
        return job
    if state == "blocked":
        job["steps"] = [step("check")]
        job.update(state="failed", current_step="universe", failure={"kind": "blocked", "error": "http_403", "at": hb,
                                            "retry_after": iso(now + dt.timedelta(hours=24))})
        guard.mark_blocked(cfg, "refresh-universe", url="https://scanner.tradingview.com/global/scan", status=403,
                           reason="http_403")
        return job
    job["canary"] = {"at": hb, "status": "ok", "cost_usd": 0.00005, "fingerprint": fp}
    job["steps"] += [step("idea_en"), step("ai_check"), step("estimate", est_cost_usd=0.28)]
    if state == "screening":
        job.update(state="running", current_step="screen")
        job["approval"]["out_dirs"] = ["/tmp/x"]
        job["progress"] = {"phase": 4, "heartbeat_at": hb, "stage": "l2",
                           "stage_started_at": iso(now - dt.timedelta(seconds=30)),
                           "spent_before": 0.00005, "run_spent_usd": 0.1834,
                           "stages": {"l1": {"started_at": iso(now - dt.timedelta(minutes=2)), "done0": 0,
                                             "done": 2154, "total": 2154},
                                      "l2": {"started_at": iso(now - dt.timedelta(seconds=30)), "done0": 0,
                                             "done": 45, "total": 120}}}
        return job
    if state == "done":
        job.update(state="done", current_step="finish")
        job["steps"] += [step("screen", run_id="scr-n"), step("fetch", "skipped"), step("finish")]
        job["approval"].update(out_dirs=["/tmp/x"], costs={"scr-n": 0.2791}, ledger_usd=0.2791)
        job["result"] = {"run_id": "scr-n", "status": "ok", "summary": {"listed": 3}}
        job["page_opened"] = True
        return job
    raise ValueError(state)


def novice_result() -> dict[str, Any]:
    """A finished run with invented A-share issuers (the same as tests/test_page._novice_result)."""
    from test_page import _novice_result
    return copy.deepcopy(_novice_result())


def write_state_page(cfg, state: str, *, lang: str = "zh") -> Path:
    """The stable page of the idea in `state`, written the way the product writes it."""
    from jevscreen import page, pagestatus, quickstart as qs
    idea = IDEA_ZH
    job = job_for(state, cfg, idea=idea, lang=lang)
    qs.save_job(cfg, job)
    if state == "done":
        res = novice_result()
        res["output_dir"] = str(Path(cfg.home) / "runs" / "scr-n")
        data = page.build_page_data(res, None, lang=lang, local_names={"SZSE:399101": "青澜科技",
                                                                       "SSE:699102": "百峰铝业"},
                                    descriptions={"k2": "Baifeng makes aluminium."})
        data["live"] = page.status_of(cfg, idea, lang, data, job)
        sp = page.stable_path(cfg, idea)
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(page.render_page(data), encoding="utf-8")
        (sp.with_suffix(".run.json")).write_text(json.dumps({"run_id": "scr-n", "started_at": iso(T0)}))
    pagestatus.write(cfg, job)
    return page.stable_path(cfg, idea)
