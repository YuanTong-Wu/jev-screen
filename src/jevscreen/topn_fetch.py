"""Top-N official text on demand (plan step 4): fetch the annual report of the top-N rows that read only a profile,
optionally re-read the short CJK reports of the top-N rows deeper, then re-read those rows in layer 2.

Two flags, both OFF by default (baselines stay comparable), on `jevscreen screen` and `jevscreen eval run`:

- `--fetch-profile-only-topn N`: after the screen's report is written, the rows ranked 1..N whose layer-2 input was
  the company profile get their newest official annual report, one source at a time in the order
  BSE -> DART -> CNINFO -> EDINET (then MOPS, only with its recorded consent, and SEC, only with a contact email).
  CNINFO / DART / EDINET texts are extracted deep (sources/deep_sections.py). The first source that refuses access
  (a block) stops the whole fetch: the sources after it are not contacted ('stopped_after_block'). Every adapter
  keeps its own polite limiter, cooldown marker and caps (it runs in the child process of jevscreen.ondemand).
- `--deepen-official-topn N`: the rows ranked 1..N that read a CNINFO / DART / EDINET report extracted before the
  deep sections existed get that report re-read deeper (the same companies only, never a re-crawl).

Then the update pass of jevscreen.ondemand_cli.run_fetch runs: screen(from_run=RUN, supersedes=RUN), L1 reused at
$0, layer 2 re-read only where the evidence text changed (the fetched / deepened rows), at most
min(UPDATE_BUDGET_DEFAULT, budget left). Works on a --from-run too (unlike `--fetch-docs auto`), and replaces the
auto fetch when given (one fetch per screen). Facts only: nothing here infers anything; the new text is the
filing's own, labelled with its source and form in the layer-2 input tag.

Profile rows outside the top N keep reason 'outside_topn' in the report's gap section. EDINET needs the user's
EDINET key (else 'no_key_edinet'), DART the OpenDART key, CNINFO / BSE / MOPS a PDF reader; MOPS stays behind its
consent. No adapter is imported at module level.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import ondemand

ORDER = ("bse", "dart", "cninfo", "edinet", "mops", "sec")
DEEP_KEYS = frozenset({"cninfo", "dart", "edinet"})
DEEP_SOURCE_KEY = {"cninfo_annual_report": "cninfo", "dart_business_report": "dart", "edinet_yuho": "edinet"}
DEEP_VERSIONS = {"cninfo": "cninfo-v4", "dart": "dart-web-v2", "edinet": "edinet-v3"}   # adapters' DEEP_* constants
EDINET = ondemand.Source("edinet", "edinet_yuho", "jevscreen.sources.edinet", "edinet",
                         ("sync-edinet", "sync edinet", "edinet.sync", "sync edinet_yuho"), "edinet_code", False,
                         "edinet_key", 10, 4.0, 120.0, 10.0)
DEEP_KWARGS: dict[str, tuple[tuple[str, Any], ...]] = {"cninfo": (("kind", "full"), ("deep", True)),
                                                       "dart": (("mode", "web"), ("deep", True)),
                                                       "edinet": (("deep", True),)}
TIME_DEFAULT_S = 300.0
MODE = "topn"
CHILD_MODULE = "jevscreen.topn_fetch"

OUTSIDE = "outside_topn"          # ondemand.REASONS also holds no_key_edinet and stopped_after_block


# ---------------------------------------------------------------------------------------------------------------
# Sources: EDINET joins the on-demand set, CJK sources extract deep (parent and child alike)

def registry(deep: bool = True) -> dict[str, ondemand.Source]:
    out = dict(ondemand.SOURCES)
    out.setdefault("edinet", EDINET)
    if deep:
        for k, kw in DEEP_KWARGS.items():
            base = dict(out[k].sync_kwargs)
            base.update(kw)
            out[k] = dataclasses.replace(out[k], sync_kwargs=tuple(base.items()))
    return out


@contextlib.contextmanager
def sources_ctx(deep: bool = True):
    """ondemand.SOURCES with EDINET and the deep sync arguments, for the duration of this fetch only."""
    saved = dict(ondemand.SOURCES)
    patched = registry(deep)
    ondemand.SOURCES.clear()
    ondemand.SOURCES.update(patched)
    try:
        yield ondemand.SOURCES
    finally:
        ondemand.SOURCES.clear()
        ondemand.SOURCES.update(saved)


def child_popen(deep: bool) -> Callable[..., Any]:
    """Popen for ondemand.launch: the child runs this module's entry (EDINET and the deep arguments registered)."""
    def popen(cmd: Sequence[str], **kw: Any) -> Any:
        if ondemand._real_adapters_refused():       # tests: the real adapters (network) need a fake module map
            raise ondemand.RealAdaptersRefused(f"{ondemand.TESTING_ENV}=1: the top-N fetch would start a real "
                                               f"adapter (set {ondemand.MODULES_ENV})")
        cmd = list(cmd)
        i = cmd.index("jevscreen.ondemand")
        cmd[i] = CHILD_MODULE
        if deep:
            cmd.insert(i + 2, "--deep")
        return subprocess.Popen(cmd, **kw)
    return popen


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] != ["child"]:
        print("usage: python -m jevscreen.topn_fetch child [--deep] --source KEY --plan PLAN.json --home HOME",
              file=sys.stderr)
        return 1
    rest = argv[1:]
    deep = "--deep" in rest
    rest = [a for a in rest if a != "--deep"]
    ondemand.SOURCES.update(registry(deep))
    return ondemand.child_main(rest)


# ---------------------------------------------------------------------------------------------------------------
# Selection (from the finished result's ranked rows)

def top_rows(result: Mapping[str, Any], n: int) -> list[dict[str, Any]]:
    return [r for r in (result.get("rows") or [])[:max(0, int(n or 0))] if isinstance(r, dict)]


def profile_only(result: Mapping[str, Any], n: int) -> list[str]:
    """company_keys of the rows ranked 1..n whose layer-2 input was the profile (rank order)."""
    return [r["company_key"] for r in top_rows(result, n) if r.get("l2_evidence") == "profile" and r.get("company_key")]


def shallow_cjk(result: Mapping[str, Any], n: int) -> list[dict[str, Any]]:
    """The rows ranked 1..n that read a CNINFO / DART / EDINET report (candidates for the deep re-read)."""
    return [r for r in top_rows(result, n) if r.get("l2_evidence") == "annual_report"
            and r.get("filing_source") in DEEP_SOURCE_KEY and r.get("company_key")]


# ---------------------------------------------------------------------------------------------------------------
# Plan

def _edinet_ready(cfg: Any) -> bool:
    try:
        return bool(cfg.edinet_api_key())
    except Exception:  # noqa: BLE001
        return False


def _paused_sources(cfg: Any, con: Any = None, now: Any = None) -> dict[str, dict[str, Any]]:
    """{source key: block record} of the sources paused by a recent block: the cooldown marker file or, like
    ondemand.plan, a 'blocked' sync run in the runs table (con: an open read-only session)."""
    from . import guard, store
    now = now or store.now_utc()
    out: dict[str, dict[str, Any]] = {}
    for key, s in registry().items():
        hit = guard.recent_block_marker(cfg, s.commands[0])
        if not hit and con is not None:
            with contextlib.suppress(Exception):
                hit = ondemand._runs_block(con, s.commands, now)
        if hit:
            out[key] = hit
    return out


def _gate(cfg: Any, key: str, rd: Mapping[str, Any], paused: Mapping[str, Any] | None = None) -> str | None:
    """The skip reason of a source before any request (None: ready)."""
    s = registry()[key]
    if s.needs_pdf and not rd.get("pdf_reader"):
        return "no_pdf_reader"
    if s.needs == "opendart" and not rd.get("opendart"):
        return "no_key_dart"
    if s.needs == "edinet_key" and not _edinet_ready(cfg):
        return "no_key_edinet"
    if s.needs == "sec_email" and not rd.get("sec_email"):
        return "no_key_sec"
    if key in (_paused_sources(cfg) if paused is None else paused):
        return "source_paused"
    return None


def deep_state(cfg: Any, company_keys: Iterable[str], con: Any = None) -> dict[str, str]:
    """{company_key: 'deep' | 'same'} of the companies whose document, seen through their deep view, is a readable
    deep text: 'deep' when it adds text to the shallow text of the same report (or there is none), 'same' when its
    text is identical (nothing found to add). Companies without a readable deep text are left out. con: an open
    read-only session (else one is opened; a locked store gives {})."""
    from . import screen, store
    from .sources import deep_sections as ds
    cks = [str(c) for c in company_keys if c]
    if not cks:
        return {}
    if con is None:
        try:
            with store.session(cfg, read_only=True, wait_s=ondemand.PLAN_WAIT_S) as c:
                return deep_state(cfg, cks, c)
        except store.StoreLocked:
            return {}
    with ds.deep_view(cks):
        docs = screen.load_documents(con, company_keys=cks)
    deep = {ck: d for ck, d in docs.items() if ds.is_deep(d) and ondemand._readable(d)}
    ids = sorted({x for d in deep.values() for x in (d["doc_id"], ds.report_key(d) + ":business")})
    sha = dict(con.execute("SELECT doc_id, text_sha256 FROM documents WHERE list_contains(?::VARCHAR[], doc_id)",
                           [ids]).fetchall()) if ids else {}
    out = {}
    for ck, d in deep.items():
        twin = sha.get(ds.report_key(d) + ":business")
        out[ck] = "same" if twin is not None and twin == sha.get(d["doc_id"]) else "deep"
    return out


def _store_facts(cfg: Any, pl: ondemand.Plan, cks: Sequence[str], sids: Sequence[str]) -> dict[str, Any]:
    """One read-only session: the paused sources, the deep state of `cks`, the companies whose deep re-read failed
    within SKIP_CACHE_DAYS, and the EDINET crawl states of `sids` (the negative cache of ondemand.plan)."""
    from . import store
    from .sources import deep_sections as ds
    now = store.now_utc()
    out: dict[str, Any] = {"paused": {}, "deep": {}, "deep_failed": {}, "states": {}}
    try:
        with store.session(cfg, read_only=True, wait_s=ondemand.PLAN_WAIT_S) as con:
            out["paused"] = _paused_sources(cfg, con, now)
            out["deep"] = deep_state(cfg, cks, con)
            if cks:
                cut = now - dt.timedelta(days=ondemand.SKIP_CACHE_DAYS)
                for ck, at in con.execute(
                        "SELECT company_key, max(fetched_at) FROM documents WHERE section = ? AND text_path IS NULL "
                        "AND list_contains(?::VARCHAR[], company_key) GROUP BY 1", [ds.DEEP_SECTION, list(cks)]
                ).fetchall():
                    if at is not None and at >= cut:
                        out["deep_failed"][ck] = at
            if sids:
                for sid, st, at, note in con.execute(
                        "SELECT security_id, status, last_attempt_at, note FROM crawl_state WHERE source_id = ? "
                        "AND list_contains(?::VARCHAR[], security_id)", [EDINET.source_id, list(sids)]).fetchall():
                    out["states"][sid] = {"status": st, "last_attempt_at": at, "note": note}
    except store.StoreLocked:
        out["paused"] = _paused_sources(cfg, None, now)
    return out


def _known_failure(e: dict[str, Any], key: str, facts: Mapping[str, Any], skips: Mapping[str, Any]
                   ) -> tuple[str, dict[str, Any]] | None:
    """ondemand.plan's negative cache for an entry this module routes itself (EDINET): the crawl-state window or a
    remembered adapter skip -> ('known_failure', fields), else None."""
    from . import store
    now = store.now_utc()
    states = [facts["states"][ln["security_id"]] for ln in e.get("lines") or []
              if ln.get("security_id") in facts["states"]]
    latest = max(states, key=lambda st: st.get("last_attempt_at") or dt.datetime.min, default=None)
    hit = ondemand.neg_cache(latest, None, None, now)
    if hit:
        at = latest.get("last_attempt_at")
        return "known_failure", {"note": latest.get("note") or hit[0], "date": at.date().isoformat() if at else "?",
                                 "days": hit[1], "status": hit[0]}
    rec = (skips.get(key) or {}).get(e["company_key"])
    sat = ondemand._skip_at(rec)
    if sat is not None and now - sat < dt.timedelta(days=ondemand.SKIP_CACHE_DAYS):
        return "known_failure", {"note": str(rec.get("status") or "skipped_unchanged"),
                                 "date": sat.date().isoformat(), "days": ondemand.SKIP_CACHE_DAYS,
                                 "status": rec.get("status")}
    return None


def make_planner(result: Mapping[str, Any], *, topn: int = 0, deepen: int = 0, deep: bool = True
                 ) -> Callable[..., ondemand.Plan]:
    """A planner for ondemand_cli.run_fetch: ondemand.plan over the run, narrowed to the top-`topn` profile rows
    (the others: 'outside_topn'), EDINET routed when its key is set, sources ordered by ORDER; plus the top-`deepen`
    rows with a shallow CJK report (entries in by_source only, flagged 'deepen'). It plans inside the deep view of
    these companies: a deep text stored by an earlier fetch is read, not fetched again (profile rows:
    'stored_since'; deepen rows: pl.deepen_ready)."""
    sel = profile_only(result, topn)
    shallow = shallow_cjk(result, deepen)

    def planner(cfg: Any, run_id: str, *, sources: Iterable[str] | None = None, retry_failed: bool = False,
                time_s: float = TIME_DEFAULT_S, **_kw: Any) -> ondemand.Plan:
        from .sources import deep_sections
        with sources_ctx(deep), deep_sections.deep_view([*sel, *(r["company_key"] for r in shallow)]):
            pl = ondemand.plan(cfg, run_id, sources=sources, retry_failed=retry_failed, time_s=time_s)
            if pl.store_busy:
                return pl
            enabled = set(registry()) if sources is None else set(sources)
            rank = {ck: i for i, ck in enumerate(sel)}
            sids = [ln["security_id"] for ck in sel for ln in (pl.entries.get(ck) or {}).get("lines") or []
                    if (pl.entries.get(ck) or {}).get("reason") == "edinet_pack"]
            facts = _store_facts(cfg, pl, [*sel, *(r["company_key"] for r in shallow)], sids)
            skips = {} if retry_failed else ondemand.load_skips(cfg)
            by_source: dict[str, list[dict[str, Any]]] = {}
            for ck, e in pl.entries.items():
                if ck not in rank:
                    e.update(reason=OUTSIDE, note=None)
                    continue
                reason, key = e.get("reason"), e.get("source")
                if reason in (None, "deferred_cap") and key:
                    e["reason"] = None
                    e.pop("cap", None)
                    e["codes"] = e.get("codes") or ondemand.source_codes(key, e.get("lines") or [], {})
                    by_source.setdefault(key, []).append(e)
                elif reason == "already_stored" and ck in facts["deep"]:
                    e.update(reason="stored_since", note=None)   # stored deep by an earlier top-N fetch: read it now
                elif reason == "edinet_pack":
                    why = _gate(cfg, "edinet", pl.readiness, facts["paused"]) or \
                        (None if "edinet" in enabled else "disabled")
                    if why:
                        e.update(reason=why, source="edinet", note=None)
                        if why == "source_paused":
                            e["when"] = str(facts["paused"]["edinet"].get("blocked_at"))[:16] + " UTC"
                        continue
                    kf = _known_failure(e, "edinet", facts, skips) if not retry_failed else None
                    if kf:
                        e.update(reason=kf[0], source="edinet", **kf[1])
                        continue
                    e.update(reason=None, note=None, source="edinet",
                             codes=ondemand.source_codes("edinet", e.get("lines") or [], {}))
                    by_source.setdefault("edinet", []).append(e)
            for ents in by_source.values():
                ents.sort(key=lambda e: rank[e["company_key"]])
            # deep re-read of shallow CJK reports (not profile inputs: by_source only)
            deepen_entries: list[dict[str, Any]] = []
            pl.deepen_ready, pl.deepen_skipped = [], {}
            if shallow:
                deepen_entries = _deepen_entries(cfg, pl, shallow, enabled, facts, retry_failed)
                for e in deepen_entries:
                    by_source.setdefault(e["source"], []).append(e)
            # per-source caps, in rank order (profile rows first)
            for key, ents in by_source.items():
                cap = registry()[key].cap
                if cap is not None and len(ents) > cap:
                    for e in ents[cap:]:
                        if e.get("deepen"):
                            pl.deepen_skipped[e["company_key"]] = "deferred_cap"
                        else:
                            e.update(reason="deferred_cap", cap=cap)
                    by_source[key] = ents[:cap]
            pl.by_source = {k: by_source[k] for k in ORDER if by_source.get(k)}
            pl.skipped = [(ck, e.get("source"), e["reason"], e.get("note")) for ck, e in pl.entries.items()
                          if e.get("reason")]
            pl.deepen = [e["company_key"] for e in deepen_entries if e in pl.by_source.get(e["source"], [])]
            pl.topn = {"topn": int(topn or 0), "deepen": int(deepen or 0), "selected": list(sel),
                       "order": [k for k in ORDER if k in pl.by_source], "deep": bool(deep),
                       "deepen_ready": list(pl.deepen_ready)}
            speeds = ondemand.load_speeds(cfg)
            per = [ondemand.SOURCES[k].list_s + len(v) * ondemand.s_per_company(cfg, k, speeds)
                   for k, v in pl.by_source.items()]
            pl.est_seconds = round(min(time_s, sum(per)), 1)       # sequential: the sources add up
            pl.est_mb = round(sum(len(v) * registry()[k].mb_per_company for k, v in pl.by_source.items()), 1)
        return pl
    return planner


def _deepen_entries(cfg: Any, pl: ondemand.Plan, rows: Sequence[Mapping[str, Any]], enabled: set[str],
                    facts: Mapping[str, Any], retry_failed: bool = False) -> list[dict[str, Any]]:
    """Entries for the deep re-read. A row whose deep text is already stored is not fetched again: it goes to
    pl.deepen_ready ('deep': the update pass reads it) or, when the deep text added nothing, pl.deepen_skipped
    ('unchanged'). Others need their source ready (else pl.deepen_skipped[ck] = the reason) and no failed deep
    re-read within SKIP_CACHE_DAYS ('known_failure', unless retry_failed)."""
    out = []
    for r in rows:
        ck = r["company_key"]
        key = DEEP_SOURCE_KEY[r["filing_source"]]
        state = facts["deep"].get(ck)
        if state == "deep":
            pl.deepen_ready.append(ck)
            continue
        if state == "same":
            pl.deepen_skipped[ck] = "unchanged"
            continue
        why = ("disabled" if key not in enabled else _gate(cfg, key, pl.readiness, facts["paused"])) or \
            (None if retry_failed or ck not in facts["deep_failed"] else "known_failure")
        if why:
            pl.deepen_skipped[ck] = why
            continue
        out.append({"company_key": ck, "security_id": r["security_id"], "name": r.get("name"),
                    "country": r.get("country"), "exchange": str(r["security_id"]).split(":", 1)[0],
                    "market_cap_usd": r.get("market_cap_usd"), "source": key, "deepen": True,
                    "lines": [{"security_id": r["security_id"]}], "codes": [r["security_id"]]})
    return out


# ---------------------------------------------------------------------------------------------------------------
# Launch: one source at a time, stop at the first block

def make_launcher(*, deep: bool = True, popen: Callable[..., Any] | None = None,
                  launch: Callable[..., dict[str, Any]] | None = None) -> Callable[..., dict[str, Any]]:
    """A launcher for ondemand_cli.run_fetch: ondemand.launch once per source in the plan's order (each gets the
    time left), stopping after the first source whose child reports a block; the sources after it are recorded
    'stopped_after_block', those after the deadline or an interrupt 'deferred_time'. Returns launch()'s dict for
    the profile rows plus 'deepened': the deepen rows whose deep text is now stored and adds text to the shallow one
    (checked in the store after each source, never taken from a child's event alone), and those an earlier fetch
    already stored (pl.deepen_ready)."""
    def launcher(cfg: Any, pl: ondemand.Plan, *, time_s: float, fetch_dir: Path, retry_failed: bool = False,
                 lang: str = "en", out: Callable[[str], None] = print, mode: str = MODE,
                 on_progress: Callable[[int, int], None] | None = None, **_kw: Any) -> dict[str, Any]:
        run_one = launch or ondemand.launch
        fetch_dir = Path(fetch_dir)
        t0 = time.monotonic()
        total = pl.n_planned
        done_before = 0
        oc: dict[str, dict[str, Any]] = {ck: {"source": e.get("source"), "code": e["reason"], "note": e.get("note"),
                                              **{k: e[k] for k in ("date", "days", "cap", "when") if k in e}}
                                         for ck, e in pl.entries.items() if e.get("reason")}
        sources: dict[str, dict[str, Any]] = {}
        fetched: list[str] = []
        docs: dict[str, Any] = {}
        deepened: list[str] = list(getattr(pl, "deepen_ready", None) or [])
        deepen_codes: dict[str, str] = {**{ck: "deepened" for ck in deepened},
                                        **dict(getattr(pl, "deepen_skipped", None) or {})}
        interrupted, store_ok, stop = False, True, None
        with sources_ctx(deep):
            for i, key in enumerate(pl.by_source):
                ents = pl.by_source[key]
                left = time_s - (time.monotonic() - t0)
                if stop is None and (interrupted or left < 5.0):
                    stop = "deferred_time"
                if stop is not None:
                    for e in ents:
                        if e.get("deepen"):
                            deepen_codes[e["company_key"]] = stop
                        else:
                            oc[e["company_key"]] = {"source": key, "code": stop, "note": None}
                    sources[key] = {"status": "not_run", "stopped_reason": stop, "requests": 0, "seconds": 0.0,
                                    "s_per_company": None, "mb": 0.0, "agent_action": "none"}
                    continue
                sub = ondemand.Plan(run_id=pl.run_id, by_source={key: ents}, readiness=pl.readiness,
                                    params=pl.params, idea=pl.idea,
                                    entries={e["company_key"]: e for e in ents},
                                    order=[e["company_key"] for e in ents])
                sub_dir = fetch_dir / f"{i + 1}-{key}"

                def progress(d: int, _t: int, base: int = done_before) -> None:
                    if on_progress is not None:
                        on_progress(min(total, base + d), total)
                kw = {"popen": popen or child_popen(deep)}
                fr = run_one(cfg, sub, time_s=left, fetch_dir=sub_dir, retry_failed=retry_failed, lang=lang,
                             out=out, mode=mode, on_progress=progress, **kw)
                done_before += len(ents)
                interrupted = interrupted or bool(fr.get("interrupted"))
                store_ok = store_ok and fr.get("store_ok", True)
                sources.update((fr.get("summary") or {}).get("sources") or {})
                # what the deep re-reads really stored (the store, not the child's 'ok' event: its write can fail)
                now_deep = deep_state(cfg, [e["company_key"] for e in ents if e.get("deepen")])
                for e in ents:
                    ck = e["company_key"]
                    o = (fr.get("outcomes") or {}).get(ck) or {"source": key, "code": "unresolved", "note": None}
                    if e.get("deepen"):
                        st = now_deep.get(ck)
                        code = "deepened" if st == "deep" else "unchanged" if st == "same" else \
                            (o["code"] if o["code"] not in ("fetched", "stored_since") else "not_stored")
                        deepen_codes[ck] = code
                        if code == "deepened":
                            deepened.append(ck)
                        continue
                    oc[ck] = o
                    if o["code"] == "fetched":
                        fetched.append(ck)
                        if ck in (fr.get("docs_by_ck") or {}):
                            docs[ck] = fr["docs_by_ck"][ck]
                if (sources.get(key) or {}).get("status") == "blocked":
                    stop = "stopped_after_block"
        seconds = round(time.monotonic() - t0, 1)
        summary = ondemand.fetch_summary(pl, oc, {}, {}, {}, seconds=seconds, time_s=time_s,
                                         interrupted=interrupted, mode=mode)
        summary["sources"] = {k: sources.get(k) or {"status": "unknown"} for k in pl.by_source}
        deferred = any(o.get("code") in ondemand.DEFERRED or o.get("code") == "stopped_after_block"
                       for o in oc.values())
        bad = any(v.get("status") != "ok" for v in summary["sources"].values())
        summary["status"] = "interrupted" if interrupted else ("partial" if (bad or deferred) else "ok")
        summary["topn"] = dict(getattr(pl, "topn", {}) or {}, deepened=list(deepened),
                               deepen_outcomes=dict(deepen_codes), stopped=stop)
        if not store_ok:
            summary["update"] = {"skipped": "store_busy"}
        return {"summary": summary, "outcomes": oc, "fetched": fetched, "docs_by_ck": docs,
                "interrupted": interrupted, "store_ok": store_ok, "deepened": deepened}
    return launcher


# ---------------------------------------------------------------------------------------------------------------
# Console text (the run's language only)

def start_text(pl: ondemand.Plan, lang: str, time_s: float) -> str:
    info = getattr(pl, "topn", {}) or {}
    n_sel = len(info.get("selected") or [])
    n_deep = len(getattr(pl, "deepen", []) or [])
    planned = {k: len(v) for k, v in pl.by_source.items()}
    order = " → ".join(ondemand.label(k, lang) for k in pl.by_source)
    rest = sum(1 for ck in (info.get("selected") or []) if (pl.entries.get(ck) or {}).get("reason"))
    per = " · ".join(f"{ondemand.label(k, lang)} {n}" for k, n in planned.items())
    dur = ondemand._dur(time_s, lang)
    n_top, n_dp = int(info.get("topn") or 0), int(info.get("deepen") or 0)
    n_ready = len(info.get("deepen_ready") or [])
    if lang == "zh":
        if n_top and n_dp and n_top != n_dp:
            head = f"补抓前 {n_top} 名的年报原文、加读前 {n_dp} 名的年报：{n_sel} 家只读了简介"
        else:
            head = f"补抓前 {n_top or n_dp} 名的年报原文：{n_sel} 家只读了简介"
        if n_deep:
            head += f"，{n_deep} 家年报原文较短、这次加读经营分析和收入构成"
        if n_ready:
            head += f"，{n_ready} 家的加读内容之前已存到本地、直接读"
        if not planned:
            return head + "；这次没有可以下载的" + (f"（{rest} 家的原因见报告“年报原文与缺口”）" if rest else "")
        line = head + f"；按 {order} 的顺序从官方网站免费下载（最多 {dur}），任何一个网站拒绝访问就全部停止：{per}"
        return line + (f"；另外 {rest} 家这次不抓（见报告“年报原文与缺口”）" if rest else "")
    if n_top and n_dp and n_top != n_dp:
        head = (f"Fetching annual reports for the top {n_top} and reading the top {n_dp} deeper: {n_sel} read the "
                f"profile only")
    else:
        head = f"Fetching annual reports for the top {n_top or n_dp}: {n_sel} read the profile only"
    if n_deep:
        head += f", {n_deep} have a short annual-report text to read deeper (business review and revenue breakdown)"
    if n_ready:
        head += f", {n_ready} already have the deeper text stored (read as is)"
    if not planned:
        return head + "; none can be downloaded this time" + (
            f" ({rest}: see 'Annual-report evidence and gaps' in the report)" if rest else "")
    line = head + (f"; downloading from official sites in the order {order} (free, at most {dur}); the first site "
                   f"that refuses access stops the whole fetch: {per}")
    return line + (f"; {rest} are not fetched this time (see 'Annual-report evidence and gaps' in the report)"
                   if rest else "")


OFF_WINS = ("补抓前几名的年报原文：跳过（这次指定了 --fetch-docs off，不下载任何年报）",
            "Fetching annual reports for the top rows: skipped (--fetch-docs off was given: nothing is downloaded)")
SKIP = {"none": ("补抓前几名的年报原文：不需要（前几名都读了年报原文）",
                 "Fetching annual reports for the top rows: not needed (every top row read an annual report)"),
        "status": ("补抓前几名的年报原文：跳过（这次筛选没有完成，先处理上面的问题）",
                   "Fetching annual reports for the top rows: skipped (the screen did not finish; see above)")}


# ---------------------------------------------------------------------------------------------------------------
# Entry points: screen phase 2 and eval

def wanted(args: Any) -> bool:
    return bool(getattr(args, "fetch_profile_only_topn", 0) or getattr(args, "deepen_official_topn", 0))


def _count(text: str) -> int:
    try:
        v = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {text!r}") from None
    if not 0 <= v <= 200:
        raise argparse.ArgumentTypeError("must be between 0 and 200")
    return v


def _seconds(text: str) -> float:
    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not 0 < v <= ondemand.FETCH_TIME_MAX_S:
        raise argparse.ArgumentTypeError(f"must be above 0 and at most {ondemand.FETCH_TIME_MAX_S:.0f} seconds")
    return v


def add_flags(p: argparse.ArgumentParser) -> None:
    """--fetch-profile-only-topn / --deepen-official-topn / --topn-fetch-time (screen and eval run; default off)."""
    p.add_argument("--fetch-profile-only-topn", type=_count, default=0, metavar="N",
                   help="lever (off by default): after the report, fetch the official annual report of the top-N rows "
                        "that read only a profile (BSE, DART, CNINFO, EDINET in that order, one at a time; MOPS only "
                        "with its consent; stops at the first site that refuses access; free) and re-read those rows "
                        "(L1 reused, at most $0.05)")
    p.add_argument("--deepen-official-topn", type=_count, default=0, metavar="N",
                   help="lever (off by default): re-read the short CNINFO / DART / EDINET reports of the top-N rows "
                        "deeper (business review, revenue breakdown; those companies only, free) and re-read the rows")
    p.add_argument("--topn-fetch-time", type=_seconds, default=TIME_DEFAULT_S, metavar="SECONDS",
                   help=f"at most this long for that fetch (default {TIME_DEFAULT_S:.0f})")


def run(cfg: Any, result: dict[str, Any], *, topn: int, deepen: int = 0, time_s: float = TIME_DEFAULT_S,
        update_budget: float, out: Callable[[str], None] = print, jev_factory: Any = None,
        deep: bool = True, launch: Callable[..., dict[str, Any]] | None = None,
        popen: Callable[..., Any] | None = None, read_offset: int | None = None) -> dict[str, Any]:
    """Plan, fetch (sequential) and update a finished run: ondemand_cli.run_fetch with this module's planner,
    launcher and start line, all inside the deep view of the selected top-N companies (only they, and only here,
    read a deep text: sources/deep_sections.py). The update pass keeps the run's read_offset (a --read-offset noise
    run stays one; default: the run's params). Returns run_fetch's dict ('result' is the final result), or
    {'result': result, 'skipped': why} when there is nothing to do."""
    from . import ondemand_cli
    from .sources import deep_sections
    lang = ondemand.lang_of(result.get("idea"))
    if result.get("status") not in ("ok", "partial"):
        out(SKIP["status"][0 if lang == "zh" else 1])
        return {"result": result, "skipped": "status"}
    if not profile_only(result, topn) and not shallow_cjk(result, deepen):
        out(SKIP["none"][0 if lang == "zh" else 1])
        return {"result": result, "skipped": "none"}
    fetch_dir = Path(result["output_dir"]) / f"fetch-topn-{int(time.time())}"
    if read_offset is None:
        read_offset = int((result.get("params") or {}).get("read_offset") or 0)
    view = [*profile_only(result, topn), *(r["company_key"] for r in shallow_cjk(result, deepen))]
    with deep_sections.deep_view(view):
        return ondemand_cli.run_fetch(
            cfg, result, time_s=time_s, update_budget=update_budget, mode=MODE, out=out, fetch_dir=fetch_dir,
            jev_factory=jev_factory, planner=make_planner(result, topn=topn, deepen=deepen, deep=deep),
            launcher=make_launcher(deep=deep, popen=popen, launch=launch), start_text=start_text,
            read_offset=int(read_offset or 0))


def fetch_phase(args: argparse.Namespace, cfg: Any, result: dict[str, Any], out: Callable[[str], None] = print
                ) -> dict[str, Any]:
    """`screen` phase 2 with the top-N flags (instead of --fetch-docs auto). Never changes the exit code. An explicit
    --fetch-docs off wins: nothing is downloaded (one line says so)."""
    if getattr(args, "fetch_docs", None) == "off":
        zh, en = OFF_WINS
        out(zh if ondemand.lang_of(result.get("idea")) == "zh" else en)
        return result
    budget_left = float(result.get("budget_usd") or 0.0) - float(result.get("cost_usd") or 0.0)
    try:
        ret = run(cfg, result, topn=int(getattr(args, "fetch_profile_only_topn", 0) or 0),
                  deepen=int(getattr(args, "deepen_official_topn", 0) or 0),
                  time_s=float(getattr(args, "topn_fetch_time", TIME_DEFAULT_S) or TIME_DEFAULT_S),
                  update_budget=min(ondemand.UPDATE_BUDGET_DEFAULT, max(0.0, budget_left)), out=out)
    except (ValueError, OSError) as e:
        print(f"warning: top-N annual-report fetch skipped: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
        return result
    except Exception as e:  # noqa: BLE001 - a fetch problem never changes the screen's result or exit code
        from . import store
        if isinstance(e, store.StoreLocked):
            out(ondemand_cli_skip_store(ondemand.lang_of(result.get("idea"))))
        else:
            print(f"warning: top-N annual-report fetch stopped: {type(e).__name__}: {str(e)[:200]}; the first "
                  "report is kept", file=sys.stderr)
        return result
    return ret["result"]


def ondemand_cli_skip_store(lang: str) -> str:
    from . import ondemand_cli
    return ondemand_cli._skip_line("store_busy", lang)


# ---------------------------------------------------------------------------------------------------------------
# eval run: the same flags (eval_cli adds them); --from-run is jevscreen.evalfrom (L1 never re-run)

def parse_from_runs(value: str | None) -> dict[str, str]:
    """{idea id: run id} of an eval --from-run SPEC (evalfrom.base_runs)."""
    from . import evalfrom
    return evalfrom.base_runs(value)


def store_drift(cfg: Any, base_run: str, run_id: str) -> list[str]:
    """company_keys whose layer-2 input differs between a --from-run screen and its base run (the source kind or the
    document URL): the store changed in between (a sync, an on-demand fetch), so that screen is not a clean replay
    of the base. [] when the store cannot be read."""
    from . import store
    q = ("SELECT company_key, input_source, evidence_url FROM screen_results WHERE run_id = ? AND layer = 'l2' "
         "AND status = 'ok'")
    try:
        with store.session(cfg, read_only=True, wait_s=ondemand.PLAN_WAIT_S) as con:
            a = {r[0]: (r[1], r[2]) for r in con.execute(q, [base_run]).fetchall()}
            b = {r[0]: (r[1], r[2]) for r in con.execute(q, [run_id]).fetchall()}
    except Exception:  # noqa: BLE001 - a check only
        return []
    return sorted(ck for ck in a.keys() & b.keys() if a[ck] != b[ck])


def score_extras(res: Mapping[str, Any]) -> dict[str, Any]:
    """Fields an eval score entry carries from this module (only when present): the top-N fetch record and the
    store drift against the base run."""
    return {k: res[k] for k in ("topn_fetch", "store_drift") if res.get(k)}


def after_eval_screen(cfg: Any, args: Any, res: dict[str, Any], *, budget_left: float,
                      out: Callable[[str], None] | None = None) -> dict[str, Any]:
    """After one eval idea's screen: the store-drift check of a --from-run screen (a warning, and 'store_drift' on
    the result), then the top-N fetch + update pass when a flag asks for it (else `res`). The update pass keeps the
    screen's read_offset. The returned result's cost_usd is the screen's plus the update's (what the idea spent). A
    failure of the fetch or its update never costs the screen already paid for: `res` is kept (scored) and the
    error recorded in res['topn_fetch']['error']."""
    base = ((res.get("layers") or {}).get("l1") or {}).get("from_run")
    if base and res.get("run_id") and res.get("status") in ("ok", "partial"):
        drift = store_drift(cfg, str(base), str(res["run_id"]))
        if drift:
            res = dict(res, store_drift={"base_run": base, "companies": drift[:50], "n": len(drift)})
            print(f"warning: {len(drift)} companies read another layer-2 input than base run {base} (the store "
                  f"changed since it ran), so this screen is not a clean replay: {', '.join(drift[:5])}"
                  + (" ..." if len(drift) > 5 else ""), file=sys.stderr)
    if not wanted(args) or res.get("status") not in ("ok", "partial"):
        return res
    lines: list[str] = []
    say = out or (lines.append if getattr(args, "json", False) else print)
    try:
        ret = run(cfg, res, topn=int(getattr(args, "fetch_profile_only_topn", 0) or 0),
                  deepen=int(getattr(args, "deepen_official_topn", 0) or 0),
                  time_s=float(getattr(args, "topn_fetch_time", TIME_DEFAULT_S) or TIME_DEFAULT_S),
                  update_budget=min(ondemand.UPDATE_BUDGET_DEFAULT, max(0.0, budget_left)), out=say,
                  read_offset=int((res.get("params") or {}).get("read_offset") or 0))
    except KeyboardInterrupt:
        raise
    except Exception as e:  # noqa: BLE001 - like fetch_phase: the paid screen is kept and scored
        print(f"warning: top-N annual-report fetch stopped: {type(e).__name__}: {str(e)[:200]}; the screen's own "
              "result is scored", file=sys.stderr)
        return dict(res, topn_fetch={"base_run": res.get("run_id"), "update_cost_usd": 0.0,
                                     "error": f"{type(e).__name__}: {str(e)[:200]}"})
    final = ret.get("result") or res
    if final is res:
        return res
    upd = (ret.get("update") or {}).get("cost_usd") if final.get("run_id") != res.get("run_id") else 0.0
    final = dict(final)
    final["cost_usd"] = round(float(res.get("cost_usd") or 0.0) + float(upd or 0.0), 6)
    final.setdefault("topn_fetch", {"base_run": res.get("run_id"), "update_cost_usd": upd})
    if res.get("store_drift"):
        final["store_drift"] = res["store_drift"]
    return final


if __name__ == "__main__":
    sys.exit(main())
