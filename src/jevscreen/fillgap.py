"""Profile gaps for quickstart: which missing companies the fill question names, whether a running default fill has
already covered the job's (raised) floor, and the small top-gap fill of the largest companies with no profile.

Split out of quickstart.py (its size limit); quickstart re-exports fill_examples / idea_match_words. Tests patch
quickstart's names (quickstart._local_names, quickstart.FILL_NAMES), so they are looked up there at call time.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

TOP_FILL_N = 50     # the top-gap fill: at most this many of the largest companies with no profile (about 30 s)

_WORD = re.compile(r"[A-Za-z][A-Za-z\-]{3,}")


def idea_match_words(terms: list[str]) -> list[str]:
    """The words a missing company's name or industry is matched against for the fill question's examples: the
    idea's CJK pieces (2+ characters) and its English content words (>= 4 letters, no generic ones such as
    'suppliers' or 'systems'), a plural 's' dropped ('joints' -> 'joint')."""
    from . import page
    out: list[str] = []
    for t in terms or []:
        t = str(t)
        cjk = page._CJK_RUN.findall(t)
        if cjk:
            out += [c for c in cjk if len(c) >= 2 and c not in page.GENERIC_CJK]
            continue
        for w in _WORD.findall(t):
            w = w.lower()
            if w in page.GENERIC_EN:
                continue
            out.append(w[:-1] if w.endswith("s") and not w.endswith("ss") and len(w) > 4 else w)
    return list(dict.fromkeys(out))


def fill_examples(con, run_id: str | None, missing: list[tuple],
                  terms: list[str] | None = None) -> tuple[list[dict[str, Any]], str]:
    """The missing companies the fill question names (at most FILL_NAMES) and why ('idea_words' | 'related' |
    'largest'): first those whose name (English or the official Chinese short name) or industry carries the idea's
    own words (most words first, then market cap: e.g. a thermal-management maker for a liquid-cooling idea); else
    those in the industries of the run's L1 passes (most passes first, then market cap), never a bank or insurer
    unless the passes include finance; else the largest non-financial ones. `missing` rows: (security_id, name,
    country, exchange, market_cap_usd, described, sector, industry), largest first."""
    from collections import Counter
    from . import quickstart as qs, screen
    n_names = qs.FILL_NAMES

    def view(r: tuple, zh: dict[str, str]) -> dict[str, Any]:
        return {"name": r[1], "name_en": qs.plain_name(r[1]), "name_zh": zh.get(r[0]) or qs.plain_name(r[1]),
                "ticker": r[0].split(":", 1)[-1], "security_id": r[0], "market_cap_usd": r[4], "industry": r[7]}
    words = idea_match_words(terms or [])
    if words and missing:
        zh = qs._local_names(con, [r[0] for r in missing])

        def hits(r: tuple) -> int:
            hay = " ".join(str(x or "") for x in (r[1], zh.get(r[0]), r[6], r[7])).lower()
            return sum(1 for w in words if w in hay)
        scored = sorted(((hits(r), r) for r in missing), key=lambda t: (-t[0], -(t[1][4] or 0), t[1][0]))
        picked = [r for n, r in scored if n > 0][:n_names]
        if picked:
            return [view(r, zh) for r in picked], "idea_words"
    ind: Counter = Counter()
    sectors: set = set()
    if run_id:
        for label, probs, status, sector, industry in con.execute(
                """SELECT r.label, r.probs_json, r.status, u.sector, u.industry FROM screen_results r
                   JOIN universe u ON u.company_key = r.company_key
                   WHERE r.run_id = ? AND r.layer = 'l1'""", [run_id]).fetchall():
            try:
                pr = json.loads(probs) if probs else {}
            except ValueError:
                pr = {}
            if screen.l1_passes({"status": status, "label": label, "probs": pr}):
                if industry:
                    ind[industry] += 1
                if sector:
                    sectors.add(sector)
    pool = [r for r in missing if "Finance" in sectors or r[6] != "Finance"]
    related = sorted((r for r in pool if r[7] and ind[r[7]]), key=lambda r: (-ind[r[7]], -(r[4] or 0)))
    basis, picked = ("related", related[:n_names]) if related else ("largest", pool[:n_names])
    local = qs._local_names(con, [r[0] for r in picked])
    return [view(r, local) for r in picked], basis


def floor_covered(cfg, market: str, fill_floor: Any, job_floor: Any, wait_s: float = 2.0) -> bool:
    """Novice #4 P1-2: the default fill started at one floor ($1B) and the human then raised the job's floor ($5B):
    once the crawl queue of the market at the NEW floor is empty (the crawl goes largest first, so those come
    first), the rest of the fill only covers companies this read will not look at, and it can stop. False when the
    floor did not rise or the store cannot be read (the fill is simply waited for, as before)."""
    from . import store
    from .sources import tradingview_profiles as tv
    try:
        if job_floor is None or fill_floor is None or float(job_floor) <= float(fill_floor) * 1.0001:
            return False
        with store.session(cfg, read_only=True, wait_s=wait_s) as con:
            return not tv._queue_rows(con, countries=[market], min_mcap_usd=float(job_floor), limit=1)
    except Exception:  # noqa: BLE001 - a busy store or an old schema: keep waiting
        return False


def top_gap_fill(cfg, job: dict[str, Any], deps: Any, n: int = TOP_FILL_N) -> dict[str, Any]:
    """Novice #4 P1-7: before the first read, the `n` largest companies of the idea's scope (its --countries, else
    every market) at the job's floor that have no profile at all (e.g. Doosan Enerbility, $38B) get one from
    TradingView: the same polite crawl-descriptions path as the default fill (consent, 24 h cooldown, rate lock,
    runs journal; stops at the first 403 / 429 / challenge, no retry). About 30 s; nothing to do on a later idea.
    Refused in the test suite unless the test passes deps.top_crawl. Returns {'status', 'exit', ...}."""
    from . import cli, ops
    from .sources import tradingview_profiles
    crawl = getattr(deps, "top_crawl", None)
    if crawl is None:
        if os.environ.get("JEVSCREEN_TESTING") == "1":
            return {"status": "testing_refused", "exit": None}
        crawl = tradingview_profiles.crawl
    countries = [str(c) for c in job.get("countries") or [] if str(c).strip()] or None
    floor = float(job["min_mcap_usd"])
    client = (getattr(deps, "crawl_client", None) or cli.make_crawl_client)(cfg)
    code, summ = ops.run_networked(
        "crawl-descriptions", cfg,
        lambda: crawl(cfg, client, countries=countries, min_mcap_usd=floor, limit=max(0, int(n))),
        client=client, consent_source="tradingview_profile")
    summ = summ if isinstance(summ, dict) else {}
    return {"status": summ.get("status"), "exit": code, "ok": summ.get("ok"), "n": n,
            "retry_after": summ.get("retry_after")}


def top_fill_once(worker: Any) -> None:
    """quickstart's worker, once per job, right after the free profile download (before the default fill of the
    idea's market and before any estimate): top_gap_fill, recorded in job['top_fill']. Never after the human's
    --fill-descriptions no; a block is a note (its 24 h cooldown then skips the default fill too)."""
    from . import quickstart as qs
    job = worker.job
    if job.get("top_fill") or job.get("fill_pref") == "no":
        return
    got = top_gap_fill(worker.cfg, job, worker.d)
    with worker.mu:
        job["top_fill"] = {**got, "at": qs.iso(qs.now_utc())}
        worker.save()
    if got.get("status") not in ("ok", "testing_refused", None):
        worker.note(f"top profile fill: {got.get('status')}; the screen goes on")
