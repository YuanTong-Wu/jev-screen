"""`jevscreen why`: why a company is (not) in a screen's result, in one plain sentence, then the facts.

Read-only, free, no network, no Jev. Sources, in this order:
- the run's funnel.jsonl.gz (the ledger: pre-L1 stage, shells rule, L1 / L2 answer per universe company);
- its results.json (rows, unverified, excluded_by_user, gaps, calibration, params, questions);
- the store, when its lock is free within --wait seconds: every company L2 read (calib.load_pool), the full ranking
  (calib.rerank_result), the L1 input text, the alias lists (jevscreen.names). When the store stays locked the answer
  comes from the files alone (status 'partial_files_only').

An explanation separates fact (what the data says), inference (what the model judged), gap (what is missing) and
the changes that would move the company, each with its exact command (argv, never the idea text), its cost and
whether the human must say yes first (ask_human: any paid, networked or pinning step).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math
import shlex
from pathlib import Path
from typing import Any

from . import l10n, screen

WAIT_S = 5.0
RECENT_DAYS = 7
L1_UNIT_FALLBACK, L2_UNIT_FALLBACK = 0.0000345, 0.00007
FROM_RUN_SECONDS = 60
QUOTE_MAX_CHARS = 200
MISSING_TERMS_MAX = 5
STAGE_IDS = ("not_found", "ambiguous", "not_in_universe", "null_mcap", "below_min_mcap", "below_min_volume",
             "other_country", "shell", "no_description", "dry_run", "l1_not_sent", "l1_rejected", "l2_not_sent",
             "l2_failed", "l2_contradicted", "l2_unverified", "ranked_below_cut", "excluded_by_user", "in_output",
             "forced_extra", "pre_ledger")
# exchange -> (sync command, key it needs or None, seconds, extra flags); per-company syncs only (never sync-sec: it
# has no --codes). sync-mops needs --mode annual: its default 'basic' stores the 主要經營業務 profile, not the report
SYNC_BY_EXCHANGE: dict[str, tuple[str, str | None, int, tuple[str, ...]]] = {
    "SSE": ("sync-cninfo", None, 30, ()), "SZSE": ("sync-cninfo", None, 30, ()), "BJSE": ("sync-cninfo", None, 30, ()),
    "TSE": ("sync-edinet", "edinet", 20, ()), "KRX": ("sync-dart", "opendart", 20, ()),
    "TWSE": ("sync-mops", None, 15, ("--mode", "annual")), "TPEX": ("sync-mops", None, 15, ("--mode", "annual")),
    "BSE": ("sync-bse", None, 40, ())}
US_EXCHANGES = frozenset({"NYSE", "NASDAQ", "AMEX", "NYSEARCA", "NYSEAMERICAN", "BATS", "CBOE", "OTC"})
# shells pattern ids in plain words (the ids stay in the stage data)
PATTERN_ZH = {"name_acq": "公司名里有「Acquisition Corp」这类字样", "name_spac": "公司名里有「SPAC」",
              "name_merger": "公司名里有「Merger Corp」", "name_special": "公司名里有「Special Purpose Acquisition」",
              "text_blank_check": "简介开头说它是空壳收购公司（blank check company）"}
PATTERN_EN = {"name_acq": "the name contains \"Acquisition Corp\" or similar", "name_spac": "the name contains \"SPAC\"",
              "name_merger": "the name contains \"Merger Corp\"",
              "name_special": "the name contains \"Special Purpose Acquisition\"",
              "text_blank_check": "the profile opens by saying it is a blank-check company"}
SPAC_LIKE_ZH = "它看起来像空壳收购公司，但有营业收入或者行业不是金融壳，所以没有排除（这条规则只排除没有收入的金融壳）"
SPAC_LIKE_EN = ("It looks like a blank-check company, but it has revenue or is not in a shell industry, so it was not "
                "removed (the rule removes only revenue-less financial shells)")
LABEL_ZH = {"core": "核心", "adjacent": "相关", "unrelated": "无关", "insufficient": "说不清",
            "explicit": "明确符合", "partial": "部分相关", "contradicted": "年报否认"}
EVIDENCE_ZH = {"annual_report": "年报原文", "profile": "公司简介"}
LABEL_EN = {"core": "central", "adjacent": "related", "unrelated": "unrelated", "insufficient": "unclear",
            "explicit": "clearly fits", "partial": "related", "contradicted": "contradicted by the text"}
EVIDENCE_EN = {"annual_report": "annual report", "profile": "company profile"}
# run states of one company in plain words (never an internal code such as skipped_budget in the text)
STATE_WORDS = {"skipped_budget": ("预算用完没读", "budget ran out"), "failed": ("出错", "failed"),
               "uncertain": ("结果不确定", "outcome unknown"), "not_sent": ("没发出", "not sent"),
               "no_excerpt": ("年报里没找到相关段落", "no relevant passage in the filing"),
               "insufficient": ("说不清", "unclear"), "not_run": ("没读", "not read"), "ok": ("完成", "done")}
LANG_WORDS = {"zh": ("中文", "Chinese"), "ja": ("日文", "Japanese"), "ko": ("韩文", "Korean"), "en": ("英文", "English")}


def _lab_zh(v: Any) -> str:
    return LABEL_ZH.get(v, STATE_WORDS.get(v, (v,))[0]) if v else "-"


def _lab_en(v: Any) -> str:
    return LABEL_EN.get(v, STATE_WORDS.get(v, (None, v))[1]) if v else "-"


class WhyError(ValueError):
    """A run could not be chosen: status is 'unknown_run' | 'choose_run' | 'no_runs'."""

    def __init__(self, status: str, text_zh: str, text_en: str, **extra: Any):
        super().__init__(text_en)
        self.status, self.text_zh, self.text_en, self.extra = status, text_zh, text_en, extra


@dataclasses.dataclass
class RunRef:
    run_id: str
    out_dir: Path | None
    idea: str
    idea_key: str
    started_at: str
    status: str
    dry_run: bool


# --------------------------------------------------------------------------------------------------- choosing a run

def _idea_key(idea: str) -> str:
    from . import keywords
    return keywords.idea_key(idea)


def list_runs(cfg, con=None) -> list[RunRef]:
    """Every run `why` can explain, newest first: screen_runs rows (when the store is readable) and the output
    directories under <home>/screens that hold a ledger (dry runs are only there)."""
    runs: dict[str, RunRef] = {}
    base = Path(cfg.home) / "screens"
    for d in sorted(base.iterdir()) if base.is_dir() else []:
        h = screen.read_ledger_header(d)
        if h and h.get("run_id"):
            runs[h["run_id"]] = RunRef(h["run_id"], d, h.get("idea") or "", h.get("idea_key") or "",
                                       str(h.get("started_at") or ""), h.get("status") or "?", bool(h.get("dry_run")))
    if con is not None:
        for run_id, idea, started, status, od in con.execute(
                "SELECT run_id, idea, started_at, status, output_dir FROM screen_runs").fetchall():
            if run_id not in runs:
                started_s = started.isoformat(timespec="seconds") if isinstance(started, dt.datetime) else str(started)
                runs[run_id] = RunRef(run_id, Path(od) if od else None, idea or "", _idea_key(idea or ""),
                                      started_s, status or "?", False)
    return sorted(runs.values(), key=lambda r: (r.started_at, r.run_id), reverse=True)


def pick_run(runs: list[RunRef], run: str | None, *, idea: str | None = None, key: str | None = None,
             now: dt.datetime | None = None) -> RunRef:
    """The run to explain: an id or output directory, else the newest run (any status) of --idea / --key, else the
    newest run when only one idea ran in the last RECENT_DAYS days (WhyError 'choose_run' otherwise)."""
    ref = (run or "latest").strip()
    if ref != "latest":
        p = Path(ref).expanduser()
        for r in runs:
            if r.run_id == ref or (r.out_dir is not None and p.exists() and r.out_dir.resolve() == p.resolve()):
                return r
        if p.is_dir() and (p / "results.json").exists():
            res = json.loads((p / "results.json").read_text(encoding="utf-8"))
            return RunRef(res["run_id"], p, res.get("idea") or "", _idea_key(res.get("idea") or ""),
                          str(res.get("started_at") or ""), res.get("status") or "?", bool(res.get("dry_run")))
        raise WhyError("unknown_run", f"没有这个筛选运行：{ref}", f"Unknown screen run: {ref}")
    want = key or (_idea_key(idea) if idea else None)
    pool = [r for r in runs if want is None or r.idea_key == want]
    if not pool:
        raise WhyError("no_runs", "还没运行过筛选" + ("（这个想法）" if want else ""), "No screen has run yet"
                       + (" for this idea" if want else ""), next_command=_cmd(["jevscreen", "doctor", "--json"]))
    if want is None:
        now = now or dt.datetime.now()
        cut = (now - dt.timedelta(days=RECENT_DAYS)).isoformat(timespec="seconds")
        recent = {r.idea_key for r in pool if r.started_at >= cut}
        if len(recent) > 1:
            seen, choices = set(), []
            for r in pool:
                if r.idea_key in seen:
                    continue
                seen.add(r.idea_key)
                choices.append({"run_id": r.run_id, "idea": r.idea[:60], "started_at": r.started_at,
                                "status": r.status, "dry_run": r.dry_run})
            raise WhyError("choose_run", "最近有几个不同的想法在筛选：请用 --run 指定是哪一次",
                           "Several ideas ran recently: pass --run", runs=choices[:5])
    return pool[0]


# ------------------------------------------------------------------------------------------------------ context

@dataclasses.dataclass
class RunCtx:
    ref: RunRef
    result: dict[str, Any]
    header: dict[str, Any] | None
    lines: dict[str, dict[str, Any]]           # company_key -> ledger line
    by_sid: dict[str, str]                     # security_id -> company_key (ledger)
    sieve: dict[str, Any] | None
    sieve_changed: bool
    db: bool
    pool: list[dict[str, Any]] | None
    inputs: dict[str, dict[str, Any]]
    index: Any = None
    descs: dict[str, str | None] = dataclasses.field(default_factory=dict)   # company_key -> L1 text (plain)
    status: str = "ok"
    con: Any = None            # the read-only store session (only while run() is inside it)
    cfg: Any = None
    led_index: Any = None
    docs: dict[str, bool] = dataclasses.field(default_factory=dict)
    requested: str | None = None     # the run asked for, when its folder now holds another run (an update of it)

    @property
    def params(self) -> dict[str, Any]:
        return self.result.get("params") or {}


def _load_result(out_dir: Path | None) -> dict[str, Any]:
    if out_dir is None:
        raise WhyError("unknown_run", "这次运行没有记录结果目录", "The run has no output directory")
    try:
        return json.loads((out_dir / "results.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise WhyError("unreadable", f"读不了 {out_dir / 'results.json'}（{type(e).__name__}）",
                       f"Cannot read {out_dir / 'results.json'} ({type(e).__name__})") from None


def load_ctx(cfg, ref: RunRef, con=None) -> RunCtx:
    """The run's files, plus the store when a read-only connection is given (else status 'partial_files_only')."""
    from . import calib
    result = _load_result(ref.out_dir)
    requested = None
    if result.get("run_id") and result["run_id"] != ref.run_id:
        # the on-demand update pass replaced this run's results.json and ledger in the same folder: explain the run
        # the files belong to (the current report) under its own id, never under the replaced run's
        requested = ref.run_id
        ref = RunRef(result["run_id"], ref.out_dir, result.get("idea") or ref.idea,
                     _idea_key(result.get("idea") or ref.idea), str(result.get("started_at") or ""),
                     result.get("status") or "?", bool(result.get("dry_run")))
    led = screen.read_ledger(ref.out_dir) if ref.out_dir else None
    header, lines = (led[0], {ln["k"]: ln for ln in led[1]}) if led else (None, {})
    sv, changed = None, False
    p = (result.get("params") or {}).get("sieve_path")
    path = Path(p) if p and p != "<inline>" else calib.sieve_path(cfg, result.get("idea") or "")
    try:
        sv = calib.load_sieve(path)
        changed = sv is not None and (result.get("params") or {}).get("sieve_sha256") not in (
            None, calib.sieve_sha256(sv, path))
    except ValueError:
        sv = None
    ctx = RunCtx(ref=ref, result=result, header=header, lines=lines,
                 by_sid={ln["id"]: k for k, ln in lines.items()}, sieve=sv, sieve_changed=changed, db=False,
                 pool=None, inputs=calib.load_inputs(ref.out_dir) if ref.out_dir else {}, cfg=cfg,
                 requested=requested)
    if con is not None:
        _attach_db(ctx, con)
    else:
        ctx.status = "partial_files_only"
    return ctx


def _attach_db(ctx: RunCtx, con) -> None:
    from . import calib, names
    ctx.db = True
    if not ctx.result.get("dry_run"):
        ctx.pool = calib.load_pool(con, ctx.ref.run_id, ctx.params)
        if not ctx.lines:        # a run made before the ledger: its L1 / L2 answers from screen_results
            for ck, sid, layer, label, pj, status in con.execute(
                    "SELECT company_key, security_id, layer, label, probs_json, status FROM screen_results "
                    "WHERE run_id = ?", [ctx.ref.run_id]).fetchall():
                ln = ctx.lines.setdefault(ck, {"k": ck, "id": sid, "n": None, "s": "l1_sent", "m": None,
                                               "pre_ledger": True})
                pr = json.loads(pj) if pj else {}
                if layer == "l1":
                    res = {"status": status, "label": label, "probs": pr}
                    ln["l1"] = {"lab": label, "p": [pr.get(n) for n in ("core", "adjacent", "unrelated",
                                                                       "insufficient")],
                                "ok": screen.l1_passes(res, adjacent_min=ctx.params.get("l1_adjacent_min", 0.6),
                                                       core_min=ctx.params.get("l1_core_min", 0.0))}
                    if status != "ok":
                        ln["s"] = "l1_not_sent"
                else:
                    ln["l2"] = {"lab": label if status == "ok" else None, "st": status,
                                "pp": screen.p_pos_of(pr) if status == "ok" else None}
            ctx.by_sid = {ln["id"]: k for k, ln in ctx.lines.items()}
    ctx.index = names.alias_index(con)
    ctx.con = con


# ------------------------------------------------------------------------------------------------------ helpers

def _cmd(argv: list[str]) -> dict[str, Any]:
    return {"argv": argv, "command": shlex.join(argv)}


def _step(argv: list[str], *, cost: float = 0.0, seconds: float = 0.0, ask_human: bool | None = None,
          network: bool = False, note: tuple[str, str] | None = None) -> dict[str, Any]:
    paid = cost > 0 or network
    is_screen = len(argv) > 1 and argv[1] == "screen" and "--dry-run" not in argv
    out = {**_cmd(argv), "cost_usd": round(cost, 6), "seconds": seconds,
           "ask_human": bool(ask_human) if ask_human is not None else (paid or is_screen)}
    if note:
        out["note_zh"], out["note_en"] = note
    return out


def cost_text(usd: float) -> tuple[str, str]:
    if usd <= 0:
        return "免费", "free"
    if usd < 0.01:
        return "不到 1 分钱", "under 1 cent"
    return f"约 ${usd:.2f}", f"about ${usd:.2f}"


def _change(code: str, zh: str, en: str, steps: list[dict[str, Any]], out_zh: str, out_en: str) -> dict[str, Any]:
    cost = round(sum(s["cost_usd"] for s in steps), 6)
    secs = sum(s["seconds"] for s in steps)
    cz, ce = cost_text(cost)
    for s in steps:                 # a sieve edited after the run: the re-read is part of the change
        if s.get("note_zh"):
            zh, en = f"{zh}；{s['note_zh']}", f"{en}; {s['note_en']}"
            break
    return {"code": code, "text_zh": zh, "text_en": en, "steps": steps, "cost_usd": cost, "cost_text_zh": cz,
            "cost_text_en": ce, "seconds": secs, "ask_human": any(s["ask_human"] for s in steps),
            "outcome_zh": out_zh, "outcome_en": out_en}


def units(result: dict[str, Any]) -> tuple[float, float]:
    """(L1 cost per item, L2 cost per company read incl. band reads) measured in this run, else the fallbacks."""
    lay = result.get("layers") or {}
    f = result.get("funnel") or {}
    c1, n1 = float((lay.get("l1") or {}).get("cost_usd") or 0), int(f.get("l1_sent") or 0)
    c2, n2 = float((lay.get("l2") or {}).get("cost_usd") or 0), int((lay.get("l2") or {}).get("items") or 0)
    return (c1 / n1 if c1 > 0 and n1 else L1_UNIT_FALLBACK), (c2 / n2 if c2 > 0 and n2 else L2_UNIT_FALLBACK)


def suggest_floor(mcap: float) -> int:
    """floor(0.98 x mcap) rounded down to 2 significant digits."""
    v = math.floor(0.98 * mcap)
    if v <= 0:
        return 0
    mag = 10 ** max(0, int(math.log10(v)) - 1)
    return int(v // mag * mag)


def _money_zh(v: float | None) -> str:
    if v is None:
        return "-"
    if v >= 1e8:
        return f"${v / 1e8:,.0f} 亿" if v >= 1e9 else f"${v / 1e8:.1f} 亿"
    return f"${v / 1e4:,.0f} 万"


def _money_en(v: float | None) -> str:
    if v is None:
        return "-"
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
        if v >= div:
            return f"${v / div:.1f}{unit}"
    return f"${v:,.0f}"


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{100 * v:.0f}%"


# ------------------------------------------------------------------------------------------------------ explain

def resolve_target(ctx: RunCtx, text: str):
    """names.Match of a target: the run's own lines first (fast, exact), then the store's alias index."""
    from . import names
    led = _ledger_index(ctx)
    m = names.resolve(led, text, prefer=ctx.lines)
    if m.status == "exact" or ctx.index is None:
        return m
    m2 = names.resolve(ctx.index, text, prefer=ctx.lines)
    return m2 if m2.status != "not_found" or m.status == "not_found" else m


def _ledger_index(ctx: RunCtx):
    from . import names
    if ctx.led_index is not None:
        return ctx.led_index
    lines, by_id, by_symbol, aliases = {}, {}, {}, []
    for k, ln in ctx.lines.items():
        sid = ln["id"]
        sym = sid.rpartition(":")[2]
        lines[sid] = {"company_key": k, "name": ln.get("n"), "symbol": sym, "exchange": sid.partition(":")[0],
                      "active": True}
        by_id[sid.casefold()] = sid
        by_symbol.setdefault(sym.casefold(), []).append(sid)
        if ln.get("n"):
            aliases.append(names.Alias(ln["n"], names.normalize(ln["n"]), sid, k, "ledger"))
    by_norm: dict[str, list] = {}
    for a in aliases:
        by_norm.setdefault(a.norm, []).append(a)
    idx = names.Index(lines=lines, universe={k: ln["id"] for k, ln in ctx.lines.items()}, by_id=by_id,
                      by_symbol=by_symbol, aliases=aliases, by_norm=by_norm, sources_used=["ledger"])
    ctx.led_index = idx
    return idx


def _row(ctx: RunCtx, k: str) -> tuple[str | None, dict[str, Any] | None]:
    for where in ("rows", "excluded_by_user", "unverified"):
        for r in ctx.result.get(where) or []:
            if r.get("company_key") == k:
                return where, r
    return None, None


def _pool_row(ctx: RunCtx, k: str) -> dict[str, Any] | None:
    return next((p for p in ctx.pool or [] if p.get("company_key") == k), None)


def _l1_text(ctx: RunCtx, k: str) -> str | None:
    if k in ctx.descs:
        return ctx.descs[k]
    txt = None
    if ctx.con is not None:
        d = screen.select_description(screen.load_descriptions(ctx.con, company_keys=[k]).get(k, []))
        txt = (d or {}).get("plain")
    ctx.descs[k] = txt
    return txt


def _idea_words(ctx: RunCtx) -> list[str]:
    tb = ctx.result.get("terms_by_lang") or {"en": ctx.result.get("terms") or []}
    return [t for t in tb.get("en") or [] if t][:12]


def _base_stages(ctx: RunCtx, ln: dict[str, Any] | None, match) -> list[dict[str, Any]]:
    p = ctx.params
    floor = p.get("min_mcap_usd")
    m = (ln or {}).get("m")
    s = (ln or {}).get("s")
    st: list[dict[str, Any]] = []
    if ln is None or ln.get("pre_ledger"):
        return st
    if s == "null_mcap":
        st.append({"id": "universe", "ok": False, "text_zh": "股票池：没有市值数据", "text_en": "universe: no market cap"})
    elif s == "below_min_mcap":
        st.append({"id": "universe", "ok": False,
                   "text_zh": f"股票池：市值约 {_money_zh(m)}，低于你的门槛 {_money_zh(floor)}",
                   "text_en": f"universe: market cap about {_money_en(m)}, below your floor {_money_en(floor)}",
                   "data": {"market_cap_usd": m, "min_mcap_usd": floor}})
    elif s == "below_min_volume":
        st.append({"id": "universe", "ok": False, "text_zh": "股票池：成交量低于你的门槛",
                   "text_en": "universe: volume below your floor", "data": {"min_avg_volume": p.get("min_avg_volume")}})
    elif s == "other_country":
        st.append({"id": "universe", "ok": False, "text_zh": f"股票池：不在你选的国家/地区（{p.get('countries')}）",
                   "text_en": f"universe: outside your countries ({p.get('countries')})"})
    else:
        st.append({"id": "universe", "ok": True, "text_zh": f"股票池：市值约 {_money_zh(m)}，高于你的门槛 {_money_zh(floor)}",
                   "text_en": f"universe: market cap about {_money_en(m)}, above your floor {_money_en(floor)}",
                   "data": {"market_cap_usd": m, "min_mcap_usd": floor}})
        return st + _shell_stage(ctx, ln)
    return st


def _pattern(ev: dict[str, Any]) -> tuple[str, str]:
    pat = ev.get("pat")
    return PATTERN_ZH.get(pat, "规则命中"), PATTERN_EN.get(pat, "a rule matched")


def _shell_stage(ctx: RunCtx, ln: dict[str, Any]) -> list[dict[str, Any]]:
    from . import shells
    ev = (ln.get("e") or {}).get("spac") or {}
    pz, pe = _pattern(ev)
    if ln.get("s") == "shell":
        return [{"id": "shells", "ok": False, "text_zh": f"排除壳公司：命中规则「{shells.RULE_ZH['spac']}」（{pz}）",
                 "text_en": f"shells filter: rule {shells.RULE_EN['spac']} ({pe})",
                 "data": {"rules": ln.get("r"), "evidence": ln.get("e")}}]
    if ln.get("r"):
        why = "你点名了它" if ln.get("kept") == "protected" else "--shells keep"
        return [{"id": "shells", "ok": True, "text_zh": f"排除壳公司：像壳公司，但保留了（{why}）",
                 "text_en": f"shells filter: looks like a shell but kept ({ln.get('kept')})"}]
    if "spac_like" in (ln.get("f") or []):
        return [{"id": "shells", "ok": True, "text_zh": f"排除壳公司：{pz}，像空壳收购公司，但有收入或不是金融壳，没有排除",
                 "text_en": f"shells filter: {pe}, looks like a blank-check company but has revenue or is not a "
                            "financial shell: kept", "data": {"flags": ["spac_like"], "evidence": ln.get("e")}}]
    return [{"id": "shells", "ok": True, "text_zh": "排除壳公司：没有命中", "text_en": "shells filter: no hit"}]


def explain(ctx: RunCtx, match, *, show_text: bool = False) -> dict[str, Any]:
    """The explanation of one resolved target (see the module docstring for the shape)."""
    res = ctx.result
    run = {"run_id": ctx.ref.run_id, "idea": res.get("idea"), "started_at": res.get("started_at"),
           "status": res.get("status"), "dry_run": bool(res.get("dry_run"))}
    out: dict[str, Any] = {"target": match.query, "match": match.as_dict(), "run": run, "stop": None,
                           "plain_zh": None, "plain_en": None, "stages": [], "facts": [], "inferences": [],
                           "gaps": [], "flags": [], "changes": [], "status": ctx.status}
    if match.status in ("not_found", "ambiguous"):
        out["stop"] = match.status
        if match.status == "not_found":
            out["plain_zh"] = f"没找到「{match.query}」：请用交易所代码（例如 2330）"
            out["plain_en"] = f"No company matches {match.query!r}: use an exchange ticker (e.g. 2330)"
        else:
            c = "、".join(f"{x['security_id']} {x['name'] or ''}".strip() for x in match.candidates)
            out["plain_zh"] = f"「{match.query}」可能是：{c}；请用代码"
            out["plain_en"] = f"{match.query!r} matches {len(match.candidates)} companies: " \
                              f"{', '.join(x['security_id'] for x in match.candidates)}; use a ticker"
        out["agent_hint_en"] = ("Resolve the name to an exchange ticker yourself and retry, e.g. 2330 or TWSE:2330; "
                                "candidates are in match.candidates")
        return out
    k = match.company_key
    ln = ctx.lines.get(k)
    name = (match.name or (ln or {}).get("n")
            or ((ctx.index.lines.get(match.security_id) or {}).get("name") if ctx.index is not None else None)
            or match.security_id)
    who_zh, who_en = f"{name}（{match.security_id}）", f"{name} ({match.security_id})"
    out["who_zh"], out["who_en"] = who_zh, who_en
    if match.matched_line:
        out["facts"].append({"zh": f"你说的 {match.matched_line} 不是主要上市线；筛选用的是 {match.security_id}",
                             "en": f"{match.matched_line} is not the primary line; the screen uses "
                                   f"{match.security_id}"})
    if match.status == "fuzzy":
        out["facts"].append({"zh": f"按名称近似匹配：{who_zh}（相似度 {match.score:.2f}）",
                             "en": f"matched by a similar name: {who_en} (score {match.score:.2f})"})
    if ln is None:
        if not ctx.lines and not ctx.db:
            _set(out, "pre_ledger", "这次运行早于运行记录，也读不了数据库：只能稍后再问",
                 "This run predates the ledger and the store is busy: ask again later")
            out["gaps"].append({"zh": "这次运行早于运行记录，只能解释第一层及以后", "en": "The run predates the ledger"})
            return out
        if ctx.header is None:
            _set(out, "pre_ledger", "这次运行早于运行记录，只能解释第一层及以后；它没有进入第一层",
                 "This run predates the ledger; the company never reached step 1")
            out["gaps"].append({"zh": "这次运行早于运行记录：市值、国家、简介这些前置条件查不到",
                                "en": "The run predates the ledger: the pre-L1 filters cannot be shown"})
            return out
        _set(out, "not_in_universe", "它不在股票池里（不是普通股的主要上市线，或已停止交易），所以没有被筛选",
             "It is not in the universe (not a primary common-stock line, or inactive), so it was never screened")
        return out
    out["stages"] = _base_stages(ctx, ln, match)
    if ln.get("pre_ledger"):
        out["gaps"].append({"zh": "这次运行早于运行记录，只能解释第一层及以后", "en": "The run predates the ledger: only step 1 "
                                                                         "and later can be explained"})
    out["flags"] = list(ln.get("f") or [])
    if {"st", "star_st"} & set(out["flags"]):
        from . import shells
        out["facts"].append({"zh": shells.ST_WARNING_ZH, "en": shells.ST_WARNING_EN})
    if "spac_like" in out["flags"] and not ln.get("r"):
        out["facts"].append({"zh": SPAC_LIKE_ZH, "en": SPAC_LIKE_EN})
    s = ln.get("s")
    if ln.get("x"):
        where, row = _row(ctx, k)
        if ln["x"] == "no_description" and where in ("rows", "excluded_by_user"):
            return _pinned_without_profile(ctx, out, ln, match, where, row)
        return _forced_extra(ctx, out, ln, match)
    if s in ("null_mcap", "below_min_mcap", "below_min_volume", "other_country"):
        return _pre_filter(ctx, out, ln, match)
    if s == "shell":
        return _shell(ctx, out, ln, match)
    if s == "no_description":
        out["stages"].append({"id": "input", "ok": False, "text_zh": "资料：没有公司简介",
                              "text_en": "input: no company profile"})
        _set(out, "no_description", "它没有公司简介，第一步只读简介，所以没有被筛选",
             "It has no company profile and step 1 reads only profiles, so it was not screened")
        if _has_doc(ctx, k):
            out["gaps"].append({"zh": "它有年报，但第一层只读简介", "en": "It has an annual report, but step 1 reads "
                                                                     "only profiles"})
        elif _has_doc(ctx, k) is False:
            out["gaps"].append({"zh": "没有简介，也没有年报原文", "en": "No profile and no annual-report text"})
        return out
    l1 = ln.get("l1") or {}
    src = l1.get("src") or ""
    doc = _has_doc(ctx, k)
    out["stages"].append({"id": "input", "ok": True,
                          "text_zh": "资料：第一步读公司简介" + ("（没有年报原文）" if doc is False else "；第二步读年报原文"
                                                         if doc else ""),
                          "text_en": "input: step 1 reads the profile" + (" (no annual-report text)" if doc is False
                                                                          else "; step 2 reads the annual report"
                                                                          if doc else ""),
                          "data": {"sources": src, "annual_report": doc}})
    if run["dry_run"] or s == "l1_not_sent":
        return _l1_not_sent(ctx, out, ln, match)
    return _after_l1(ctx, out, ln, match, show_text=show_text)


def _set(out: dict[str, Any], stop: str, zh: str, en: str) -> None:
    out["stop"], out["plain_zh"], out["plain_en"] = stop, zh, en


def _has_doc(ctx: RunCtx, k: str) -> bool | None:
    """Whether layer 2 had (or would have) official annual-report text for the company; None when unknown (files
    only and it was never read by L2)."""
    inp = ctx.inputs.get(k)
    if inp is not None:
        return inp.get("evidence") == "annual_report"
    p = _pool_row(ctx, k)
    if p is not None and p.get("l2_evidence"):
        return p.get("l2_evidence") == "annual_report"
    ev = ((ctx.lines.get(k) or {}).get("l2") or {}).get("ev")
    if ev:
        return ev == "annual_report"
    if ctx.con is not None:
        if k not in ctx.docs:
            ctx.docs[k] = bool(screen.load_documents(ctx.con, company_keys=[k]).get(k))
        return ctx.docs[k]
    return None


def _add_should_pass(ctx: RunCtx, sid: str) -> dict[str, Any]:
    return _step(["jevscreen", "sieve", "add", "should_pass", sid, "--run", ctx.ref.run_id], ask_human=False)


def _from_run(ctx: RunCtx, *extra: str, cost: float, edit: bool = False) -> dict[str, Any]:
    """The `screen --from-run` step (L1 reused for free) with the run's sieve (edit: the sieve the change edits);
    priced with the step-2 re-read when the sieve changed after the run (_drift). A dry run has no L1 answers to
    reuse: its step is the fresh dry run `screen --idea-of RUN ... --dry-run` instead."""
    if ctx.result.get("dry_run"):
        over = dict(zip(extra[::2], extra[1::2]))
        return _step(_fresh_argv(ctx, edit=edit, **over) + ["--dry-run"], ask_human=False)
    extra_cost, note = _reprice(ctx, _drift(ctx, edit), fresh=False)
    total = cost + extra_cost
    return _step(["jevscreen", "screen", "--from-run", ctx.ref.run_id, *extra, *_sieve_argv(ctx, edit),
                  "--budget", _budget_for(total)], cost=total, seconds=FROM_RUN_SECONDS, note=note)


def _param_argv(p: dict[str, Any], **override: Any) -> list[str]:
    """The run's filters as screen flags (for a fresh screen with --idea-of)."""
    vals = {"--min-mcap": p.get("min_mcap_usd"), "--min-volume": p.get("min_avg_volume"),
            "--countries": ",".join(p.get("countries") or []) or None, "--max-out": p.get("max_out"),
            "--l2-max": p.get("l2_max")}
    vals.update(override)
    argv = []
    for flag, v in vals.items():
        if v is not None:
            argv += [flag, f"{v:g}" if isinstance(v, float) else str(v)]
    return argv


def _opts_argv(p: dict[str, Any]) -> list[str]:
    """The run's other effective screen options that differ from a fresh screen's defaults (screen.SCREEN_DEFAULTS):
    --no-translate, --shells, --reads, --rank, --keywords*. (l1_adjacent_min / l1_core_min have no flag.)"""
    d = screen.SCREEN_DEFAULTS
    argv: list[str] = []
    if p.get("translate") is False:
        argv.append("--no-translate")
    shells = p.get("shells", "keep")          # a run made before the shells filter read every company
    if shells != d["shells"]:
        argv += ["--shells", shells]
    if p.get("reads") not in (None, d["reads"]):
        argv += ["--reads", str(p["reads"])]
    if p.get("rank") not in (None, d["rank"]):
        argv += ["--rank", str(p["rank"])]
    for key in ("keywords", "keywords_zh", "keywords_ja", "keywords_ko"):
        if p.get(key):
            argv += ["--" + key.replace("_", "-"), ",".join(p[key])]
    return argv


def _sieve_argv(ctx: RunCtx, edit: bool = False) -> list[str]:
    """The --sieve flag that gives the next run this run's sieve: none when it had none (edit: the sieve that
    `sieve add|pin --run` edits, the idea's own file), nothing for the idea's own file, else its path."""
    from . import calib
    sp = ctx.params.get("sieve_path")
    if sp in (None, "<inline>"):
        return [] if edit or sp == "<inline>" else ["--sieve", "none"]
    if Path(sp) == calib.sieve_path(ctx.cfg, ctx.result.get("idea") or ""):
        return []
    return ["--sieve", sp]


def _fresh_argv(ctx: RunCtx, *, edit: bool = False, **override: Any) -> list[str]:
    """A fresh screen of the run's idea with every option of the run (filters, translate, shells, reads, rank,
    keywords, sieve), so it asks the same questions and reuses every cached answer."""
    en = agent_idea_en(ctx.result)       # a caller-supplied idea_en is not re-derived by a fresh screen: pass it
    return ["jevscreen", "screen", "--idea-of", ctx.ref.run_id, *_param_argv(ctx.params, **override),
            *_opts_argv(ctx.params), *(["--idea-en", en] if en else []), *_sieve_argv(ctx, edit)]


def _fresh_steps(ctx: RunCtx, cost: float, seconds: float, **override: Any) -> list[dict[str, Any]]:
    """A free dry run first, then the paid run (only the dry run for a dry base: its estimate is the price)."""
    argv = _fresh_argv(ctx, **override)
    extra_cost, note = _reprice(ctx, _drift(ctx, False), fresh=True)
    total = cost + extra_cost
    dry = _step(argv + ["--dry-run", "--budget", _budget_for(total)], ask_human=False)
    if ctx.result.get("dry_run"):
        return [dry]
    return [dry, _step(argv + ["--budget", _budget_for(total)], cost=total, seconds=seconds, note=note)]


def _run_idea_en(result: dict[str, Any]) -> str | None:
    return result.get("idea_en") or (result.get("keywords") or {}).get("idea_en") or None


def _agent_params(params: dict[str, Any]) -> bool:
    return params.get("idea_en_source") == "agent" or params.get("keywords_status") == "agent"


def agent_idea_en(result: dict[str, Any]) -> str | None:
    """The idea_en the caller supplied to the run (quickstart, screen --idea-en; status 'agent'), else None."""
    return _run_idea_en(result) if _agent_params(result.get("params") or {}) else None


def next_idea_en(con, idea: str, sv: dict[str, Any] | None, params: dict[str, Any], run_idea_en: str | None
                 ) -> str | None:
    """The idea_en the next run of the idea uses with this sieve (screen.resolve_keywords' order): none for an English
    idea or --no-translate; the sieve's (or the frozen one, sieve_author.author_keywords); else the local model's,
    which gives the run's text again. A run whose idea_en the caller supplied (quickstart / --idea-en) keeps it: a
    --from-run inherits it and a fresh screen gets it as --idea-en (_fresh_argv), before the sieve's."""
    from . import sieve_author
    if _agent_params(params):
        return run_idea_en
    if not screen.needs_translation(idea) or params.get("translate") is False:
        return None
    frozen = (False, None)
    if sv and sv.get("idea_en") and con is not None:
        frozen = screen._frozen_idea_en(con, idea)
    a = sieve_author.author_keywords(sv, idea, frozen=frozen) if sv else None
    if a and (a["frozen"] or a["idea_en"]):
        return a["idea_en"]
    return run_idea_en


def drift_of(con, idea: str, result: dict[str, Any], sv: dict[str, Any] | None, *, changed: bool = True
             ) -> dict[str, Any]:
    """What a next run of `result`'s idea with sieve `sv` asks differently: {'idea_en' (next), 'l1': the idea_en
    changes (a fresh screen asks every L1 item again), 'l2': the L2 question (params.l2_question_sha) or the sieve's
    excerpt terms (params.sieve_terms_sha) change, so step 2 is read again - None when the run predates those shas
    and the sieve may have changed (`changed`)}."""
    p = result.get("params") or {}
    run_en = _run_idea_en(result)
    en = next_idea_en(con, idea, sv, p, run_en)
    l1 = (en or None) != (run_en or None)
    if p.get("l2_question_sha") and "sieve_terms_sha" in p:
        q = screen.question_sha(screen.build_l2_question(idea, en, rules=(sv or {}).get("rules") or (),
                                                         facets=(sv or {}).get("facets")))
        l2: bool | None = q != p["l2_question_sha"] or screen.sieve_terms_sha(sv) != p.get("sieve_terms_sha")
    else:
        l2 = None if changed else False
    return {"idea_en": en, "l1": l1, "l2": True if l1 else l2}


def reprice_cost(result: dict[str, Any], drift: dict[str, Any], *, fresh: bool) -> dict[str, Any]:
    """{'l1_usd', 'l2_usd', 'maybe'} of the re-asking a drift causes: every L1 item of the run (fresh screens only)
    and its whole step 2 (as much as the run spent on it)."""
    l1u, l2u = units(result)
    lay2 = (result.get("layers") or {}).get("l2") or {}
    n1 = int((result.get("funnel") or {}).get("l1_sent") or 0)
    l2_full = float(lay2.get("cost_usd") or 0) or int(lay2.get("items") or 0) * l2u
    return {"l1_usd": round(n1 * l1u, 6) if fresh and drift["l1"] else 0.0,
            "l2_usd": round(l2_full, 6) if drift["l2"] is not False else 0.0, "maybe": drift["l2"] is None}


def _next_sieve(ctx: RunCtx, edit: bool) -> dict[str, Any] | None:
    if ctx.params.get("sieve_path") is None and not edit:
        return None
    return ctx.sieve


def _drift(ctx: RunCtx, edit: bool) -> dict[str, Any]:
    changed = ctx.sieve_changed or (ctx.params.get("sieve_path") is None and edit and ctx.sieve is not None)
    return drift_of(ctx.con, ctx.result.get("idea") or "", ctx.result, _next_sieve(ctx, edit), changed=changed)


def _reprice(ctx: RunCtx, drift: dict[str, Any], *, fresh: bool) -> tuple[float, tuple[str, str] | None]:
    """(extra cost, note) of the re-asking a sieve edited after the run causes."""
    rc = reprice_cost(ctx.result, drift, fresh=fresh)
    extra = rc["l1_usd"] + rc["l2_usd"]
    if extra <= 0:
        return 0.0, None
    if rc["l1_usd"]:
        return extra, (f"idea_en 改了：第一步和第二步都要重问，约 ${extra:.2f}",
                       f"idea_en changed: steps 1 and 2 are asked again, about ${extra:.2f}")
    if rc["maybe"]:
        return extra, (f"筛子在这次运行之后改过：可能要重读第二步，最多约 ${extra:.2f}",
                       f"The sieve changed after this run: step 2 may be read again, at most about ${extra:.2f}")
    return extra, (f"筛子在这次运行之后改过（第二步的问题或关键词变了）：第二步要全部重读，约 ${extra:.2f}",
                   f"The sieve changed after this run (step-2 question or terms): step 2 is read again, about "
                   f"${extra:.2f}")


def _budget_for(cost: float) -> str:
    return f"{max(0.05, math.ceil(cost * 1.5 * 100) / 100):.2f}"


def _pre_filter(ctx: RunCtx, out: dict[str, Any], ln: dict[str, Any], match) -> dict[str, Any]:
    s, m = ln["s"], ln.get("m")
    p = ctx.params
    l1u, l2u = units(ctx.result)
    zh = {"null_mcap": "它没有市值数据，所以不在股票池里", "below_min_mcap": "它的市值低于你设的门槛，所以没有进入筛选",
          "below_min_volume": "它的成交量低于你设的门槛，所以没有进入筛选",
          "other_country": "它不在你选的国家/地区，所以没有进入筛选"}[s]
    en = {"null_mcap": "It has no market cap, so it is not in the universe",
          "below_min_mcap": "Its market cap is below your floor, so it was not screened",
          "below_min_volume": "Its volume is below your floor, so it was not screened",
          "other_country": "It is outside your countries, so it was not screened"}[s]
    _set(out, s, zh, en)
    if s == "null_mcap":
        out["gaps"].append({"zh": "没有市值数据（行情来源没给）", "en": "No market cap in the market data"})
        return out
    if s == "below_min_mcap":
        out["facts"].append({"zh": f"市值约 {_money_zh(m)}；门槛 {_money_zh(p.get('min_mcap_usd'))}",
                             "en": f"market cap about {_money_en(m)}; floor {_money_en(p.get('min_mcap_usd'))}"})
    out["changes"].append(_change(
        "add_should_pass", "把它加进「应该有（检查）」，下次运行会读它的资料并在报告里写结果",
        "Add it to \"should be there (check)\"; the next run reads its documents and reports the result",
        [_add_should_pass(ctx, match.security_id), _from_run(ctx, cost=l2u, edit=True)],
        "读证据写进报告「你关心的公司」，不进名单（它在你的筛选条件以外）",
        "Its evidence is read and shown under \"companies you care about\"; it will not enter the list (outside "
        "your filters)"))
    if s == "below_min_mcap" and m:
        new_floor = suggest_floor(m)
        n_new = sum(1 for x in ctx.lines.values() if x.get("s") == "below_min_mcap" and (x.get("m") or 0) >= new_floor)
        f = ctx.result.get("funnel") or {}
        rate = (f.get("l1_pass") or 0) / max(1, f.get("l1_sent") or 0)
        cost = n_new * l1u + math.ceil(n_new * rate) * l2u
        out["changes"].append(_change(
            "lower_min_mcap", f"把市值门槛降到 {_money_zh(new_floor)}：先免费试算，再正式运行（多筛约 {n_new} 家）",
            f"Lower the floor to {_money_en(new_floor)}: a free dry run first, then the real run (~{n_new} more "
            "companies)",
            _fresh_steps(ctx, cost, FROM_RUN_SECONDS + n_new * 0.05, **{"--min-mcap": new_floor}),
            "它会和门槛以上的公司一起被筛选（第一步照常判断）",
            "It is screened like every company above the floor (step 1 judges it as usual)"))
    if s == "other_country":
        country = None
        if ctx.con is not None:
            row = ctx.con.execute("SELECT country FROM securities WHERE security_id = ?", [ln["id"]]).fetchone()
            country = row[0] if row else None
        if country:
            argv = _fresh_argv(ctx, **{"--countries": ",".join(list(p.get("countries") or []) + [country])})
            out["changes"].append(_change(
                "add_country", f"把 {country} 加进国家/地区：先免费试算", f"Add {country} to the countries: dry run first",
                [_step(argv + ["--dry-run", "--budget", "0.05"], ask_human=False)],
                "试算会给出多筛多少家、要花多少钱", "The dry run says how many more companies and what it costs"))
    return out


def _shell(ctx: RunCtx, out: dict[str, Any], ln: dict[str, Any], match) -> dict[str, Any]:
    from . import shells
    l1u, l2u = units(ctx.result)
    ev = (ln.get("e") or {}).get("spac") or {}
    _set(out, "shell", "它看起来是空壳收购公司（SPAC：没有业务、只为并购而上市），所以在第一步之前被排除了",
         "It looks like a blank-check company (SPAC: no business, listed only to merge), so it was removed before "
         "step 1")
    pz, pe = _pattern(ev)
    out["facts"].append({"zh": f"规则 {shells.RULE_ZH['spac']}：{pz}", "en": f"rule {shells.RULE_EN['spac']}: {pe}"})
    n = (ctx.result.get("shells") or {}).get("dropped") or 1
    cost = n * l1u
    out["changes"].append(_change(
        "add_should_pass", "把它加进「应该有（检查）」：点名的公司不会被当成壳公司排除",
        "Add it to \"should be there (check)\": a company you name is never removed as a shell",
        [_add_should_pass(ctx, match.security_id), _from_run(ctx, cost=l2u, edit=True)],
        "下次运行第二步会读它的资料并在报告里写结果；第一步没问过它，所以不会进名单",
        "The next run reads its documents and reports the result; step 1 never judged it, so it will not enter the "
        "list"))
    out["changes"].append(_change(
        "shells_keep", f"整个重跑一次，不排除壳公司（多筛约 {n} 家）", f"Screen again keeping shells (~{n} more companies)",
        _fresh_steps(ctx, cost, FROM_RUN_SECONDS, **{"--shells": "keep"}),
        "所有壳公司都会被第一步判断一遍", "Every shell is judged by step 1"))
    return out


def _pinned_without_profile(ctx: RunCtx, out: dict[str, Any], ln: dict[str, Any], match, where: str,
                            row: dict[str, Any]) -> dict[str, Any]:
    """A company inside the filters without a profile that the human pinned: step 1 never saw it, step 2 read its
    annual report because of the pin, and the ranking step applied the pin (screen keeps it among the candidates)."""
    out["stages"].append({"id": "input", "ok": True, "text_zh": "资料：没有公司简介（第一步没问它）；因为你钉选了它，第二步照读了年报",
                          "text_en": "input: no profile (step 1 never asked); step 2 read it because of your pin"})
    out["facts"].append({"zh": "它没有公司简介，第一步没有问它；它进不进名单由你的钉选决定",
                         "en": "It has no profile, so step 1 never asked about it; your pin decides whether it is "
                               "listed"})
    if where == "rows" and row.get("rank") is not None:
        return _in_output(ctx, out, row)
    return _excluded(ctx, out, row, match)


def _forced_extra(ctx: RunCtx, out: dict[str, Any], ln: dict[str, Any], match) -> dict[str, Any]:
    from . import report
    l2 = ln.get("l2") or {}
    stage_zh = report.STAGE_ZH.get(ln["x"], ln["x"])
    lab = l2.get("lab")
    _set(out, "forced_extra", f"它在你的校准文件里，但{stage_zh}：只读了证据写在报告「你关心的公司」里，不进名单"
         + (f"（第二步：{LABEL_ZH.get(lab, lab)}）" if lab else ""),
         f"It is in your sieve but outside the filters ({ln['x']}): its evidence was read and reported under "
         "\"companies you care about\", not listed" + (f" (step 2: {_lab_en(lab)})" if lab else ""))
    out["stages"].append({"id": "l2", "ok": lab in ("explicit", "partial"),
                          "text_zh": f"第二步：{_lab_zh(lab or l2.get('st'))}",
                          "text_en": f"step 2: {_lab_en(lab or l2.get('st'))}"})
    return out


def _l1_not_sent(ctx: RunCtx, out: dict[str, Any], ln: dict[str, Any], match) -> dict[str, Any]:
    l1u, _ = units(ctx.result)
    if ctx.result.get("dry_run"):
        _set(out, "dry_run", "这是试算，第一层还没问模型：它会被送去第一步判断",
             "This was a dry run: step 1 has not asked the model yet; the company would be judged")
        out["stages"].append({"id": "l1", "ok": None, "text_zh": "第一步：试算，还没问", "text_en": "step 1: dry run"})
        return out
    st = (ln.get("l1") or {}).get("st") or "not_sent"
    _set(out, "l1_not_sent", "第一步没有问到它（预算用完或服务出错），所以没有结果",
         "Step 1 never answered for it (budget ran out or the provider failed), so it has no result")
    out["stages"].append({"id": "l1", "ok": False, "text_zh": f"第一步：没问到（{_lab_zh(st)}）",
                          "text_en": f"step 1: {_lab_en(st)}"})
    missing = sum(1 for x in ctx.lines.values() if x.get("s") == "l1_not_sent")
    cost = missing * l1u
    out["changes"].append(_change(
        "rerun_same", f"用同样的条件再跑一次（已有的回答免费复用；约 {missing} 家要补问）",
        f"Run again with the same filters (cached answers are free; ~{missing} companies to ask)",
        _fresh_steps(ctx, cost, FROM_RUN_SECONDS),
        "它会得到第一步的判断", "It gets a step-1 answer"))
    return out


def _l1_stage(ctx: RunCtx, ln: dict[str, Any]) -> dict[str, Any]:
    l1 = ln.get("l1") or {}
    p = l1.get("p") or [None] * 4
    core, adj, unrel, unclear = (p + [None] * 4)[:4]
    lab = l1.get("lab")
    amin = ctx.params.get("l1_adjacent_min", screen.L1_ADJACENT_MIN)
    probs_zh = f"无关 {_pct(unrel)} / 相关 {_pct(adj)} / 核心 {_pct(core)}"
    probs_en = f"unrelated {_pct(unrel)} / adjacent {_pct(adj)} / core {_pct(core)}"
    rule_zh = f"只有判为「核心」，或判为「相关」且 核心+相关 ≥ {_pct(amin)} 才通过"
    rule_en = f"passes only as core, or as adjacent with core + adjacent >= {_pct(amin)}"
    txt_zh = f"第一步：模型判为「{LABEL_ZH.get(lab, lab)}」（{probs_zh}）"
    txt_en = f"step 1: the model said {lab} ({probs_en})"
    if lab == "adjacent" and not l1.get("ok"):
        txt_zh += f"；核心+相关 = {_pct((core or 0) + (adj or 0))}，差一点"
        txt_en += f"; core + adjacent = {_pct((core or 0) + (adj or 0))}, just short"
    return {"id": "l1", "ok": bool(l1.get("ok")), "text_zh": txt_zh, "text_en": txt_en, "rule_zh": rule_zh,
            "rule_en": rule_en, "data": {"label": lab, "p_core": core, "p_adjacent": adj, "p_unrelated": unrel,
                                         "p_insufficient": unclear, "adjacent_min": amin, "source": l1.get("src")}}


def _after_l1(ctx: RunCtx, out: dict[str, Any], ln: dict[str, Any], match, *, show_text: bool) -> dict[str, Any]:
    k = match.company_key
    l1u, l2u = units(ctx.result)
    st1 = _l1_stage(ctx, ln)
    out["stages"].append(st1)
    out["inferences"].append({"zh": "以上百分比是模型的判断，模型不给理由。", "en": "The percentages are the model's "
                                                                          "judgement; it gives no reasons."})
    if _has_doc(ctx, k) is False:
        out["gaps"].append({"zh": "没有年报原文，只看了简介。", "en": "No annual-report text: only the profile was read."})
    where, row = _row(ctx, k)
    l2 = ln.get("l2") or {}
    pool = _pool_row(ctx, k) or {}
    if show_text and (ctx.inputs.get(k) or row):
        q = _quote(ctx.inputs.get(k) or {}, row)
        if q:
            out["facts"].append(q)
    if where == "rows" and row.get("rank") is not None:
        return _in_output(ctx, out, row)
    if where == "excluded_by_user":
        return _excluded(ctx, out, row, match)
    if not st1["ok"]:
        words = _idea_words(ctx)
        text = _l1_text(ctx, k)
        if text and words:
            missing = [w for w in words if not screen.matched_terms(text, [w])][:MISSING_TERMS_MAX]
            if missing:
                out["facts"].append({"zh": f"简介里没出现：{'、'.join(missing)}", "en": f"not in the profile: "
                                                                                 f"{', '.join(missing)}"})
        lab = l1_lab = (ln.get("l1") or {}).get("lab")
        plain_zh = {"unrelated": "模型只读了它的简介，认为和你的想法关系不大，所以第一步就没通过。",
                    "insufficient": "模型读了它的简介，觉得说不清楚，所以第一步没通过。",
                    "adjacent": "模型认为它和你的想法有些相关，但把握不够，所以第一步差一点没通过。"}.get(
            l1_lab, "第一步没通过。")
        plain_en = {"unrelated": "The model read only its profile and judged it unrelated, so it stopped at step 1.",
                    "insufficient": "The model found its profile too vague, so it stopped at step 1.",
                    "adjacent": "The model saw some relation but not enough, so it narrowly failed step 1."}.get(
            l1_lab, "It failed step 1.")
        _set(out, "l1_rejected", plain_zh, plain_en)
        read = "年报" if _has_doc(ctx, k) else "简介"
        if l2.get("st") or pool:
            lab2 = l2.get("lab") or pool.get("l2_label")
            out["stages"].append({"id": "l2", "ok": lab2 in ("explicit", "partial"),
                                  "text_zh": f"第二步：因为在你的检查名单里照读了：{_lab_zh(lab2)}",
                                  "text_en": f"step 2: read anyway (your check): {_lab_en(lab2)}"})
        else:
            out["stages"].append({"id": "l2", "ok": None, "text_zh": "第二步：没读（第一步没通过）",
                                  "text_en": "step 2: not read (step 1 failed)"})
        out["changes"].append(_change(
            "add_should_pass", f"把它加进「应该有（检查）」，下次运行会读它的{read}并在报告里写结果",
            "Add it to \"should be there (check)\"; the next run reads its documents and reports the result",
            [_add_should_pass(ctx, match.security_id), _from_run(ctx, cost=l2u, edit=True)],
            f"会读它的{read}并在报告里写结果，但第一层没通过，不会进名单；要进名单需要你本人决定（钉选）",
            "Its documents are read and reported, but step 1 failed, so it will not enter the list; listing it "
            "needs your own decision (pin)"))
        out["changes"].append(_pin_change(ctx, match, "yes"))
        return out
    # passed L1
    if not l2.get("st") and not pool:
        return _l2_not_sent(ctx, out, match)
    lab2 = l2.get("lab") or pool.get("l2_label")
    st2 = l2.get("st") or ("ok" if pool.get("l2_label") else pool.get("l2_status"))
    if st2 != "ok":
        _set(out, "l2_failed", "第一步通过了，但第二步没读成（预算用完或服务出错）",
             "It passed step 1, but step 2 did not complete (budget or provider)")
        out["stages"].append({"id": "l2", "ok": False, "text_zh": f"第二步：没读成（{_lab_zh(st2)}）",
                              "text_en": f"step 2: {_lab_en(st2)}"})
        out["changes"].append(_change("rerun_from_run", "从这次运行再跑一次（第一步免费复用）",
                                      "Run again from this run (step 1 reused for free)",
                                      [_from_run(ctx, cost=l2u)], "第二步会补读它", "Step 2 reads it"))
        return out
    evd = EVIDENCE_ZH.get(pool.get("l2_evidence") or l2.get("ev"), "资料")
    pp = pool.get("l2_p_pos") if pool else l2.get("pp")
    out["stages"].append({"id": "l2", "ok": lab2 in ("explicit", "partial"),
                          "text_zh": f"第二步：读了{evd}，判为「{LABEL_ZH.get(lab2, lab2)}」（明确+部分 {_pct(pp)}）",
                          "text_en": f"step 2: read the {EVIDENCE_EN.get(pool.get('l2_evidence') or l2.get('ev'), 'text')}"
                                     f", said \"{_lab_en(lab2)}\" (clearly fits + related {_pct(pp)})",
                          "data": {"label": lab2, "p_pos": pp, "reads": pool.get("l2_reads"),
                                   "p_pos_sd": pool.get("l2_p_pos_sd"), "edge": pool.get("l2_edge")}})
    inp = ctx.inputs.get(k) or {}
    if inp.get("evidence") == "annual_report":
        terms = "、".join(inp.get("matched_terms") or [])
        lw = LANG_WORDS.get(inp.get("lang") or "", (inp.get("lang") or "-",) * 2)
        out["facts"].append({"zh": f"第二步读的：年报原文（{l10n.source_words(inp.get('source_id'), 'zh') or '-'}，"
                                   f"{lw[0]}）；关键词段落：{'有' if inp.get('keyword_hit') else '没有'}"
                                   + (f"（{terms}）" if terms else ""),
                             "en": f"step 2 read: annual report ({l10n.source_words(inp.get('source_id'), 'en') or '-'}, "
                                   f"{lw[1]}); keyword paragraph: "
                                   f"{'yes' if inp.get('keyword_hit') else 'no'}" + (f" ({terms})" if terms else "")})
    if lab2 == "contradicted":
        _set(out, "l2_contradicted", "第二步读到的资料明确说它不做（或已经不做）这件事，所以被去掉了",
             "Step 2 found text saying it does not (or no longer) do this, so it was dropped")
        out["changes"].append(_pin_change(ctx, match, "yes"))
        return out
    if lab2 not in ("explicit", "partial"):
        _set(out, "l2_unverified", f"第一步通过了，但第二步读了{evd}没找到明确的证据，所以没进名单（放在「未证实」里）",
             f"It passed step 1, but step 2 found no explicit evidence in the {pool.get('l2_evidence') or 'text'}, so "
             "it is listed as unverified, not ranked")
        if pool.get("l2_edge"):
            out["inferences"].append({"zh": "边缘：再读一次可能会翻", "en": "borderline: another read could flip it"})
        if _has_doc(ctx, k) is False:
            out["changes"] += _sync_change(ctx, match, l2u)
        elif inp and not inp.get("keyword_hit"):
            out["changes"].append(_change(
                "seed_terms", "让你的 AI 在筛子 seed_terms 里加上这门语言的说法（年报里没找到关键词段落）",
                "Have your AI add this language's terms to the sieve's seed_terms (no keyword paragraph found)",
                [], "下次第二步会读到更相关的段落", "Step 2 then reads more relevant paragraphs"))
        out["changes"].append(_pin_change(ctx, match, "yes"))
        return out
    return _below_cut(ctx, out, match)


def _quote(inp: dict[str, Any], row: dict[str, Any] | None) -> dict[str, Any] | None:
    text = (row or {}).get("evidence_excerpt") or ""
    if not text:
        ex = inp.get("excerpts") or []
        text = next((e["text"] for e in ex if e.get("kind") == "keywords"), ex[0]["text"] if ex else "")
    if not text:
        return None
    ev = inp.get("evidence") or (row or {}).get("l2_evidence")
    tier = "official-private" if ev == "annual_report" else "gray-private"
    t = screen.truncate(text, QUOTE_MAX_CHARS)
    zh, en = ("年报", "annual report") if tier == "official-private" else ("简介", "profile")
    return {"zh": f"原文（{zh}，仅限个人使用）：{t}", "en": f"quote ({en}, personal use only): {t}", "tier": tier}


def _pin_change(ctx: RunCtx, match, want: str) -> dict[str, Any]:
    _, l2u = units(ctx.result)
    k = match.company_key
    read = bool((ctx.lines.get(k) or {}).get("l2")) or _pool_row(ctx, k) is not None
    return _change("pin_yes" if want == "yes" else "pin_no", "只有你本人同意，才能把它钉进名单（这是你的判断，不是证据）",
                   "Only with your own yes can it be pinned into the list (your judgement, not evidence)",
                   [_step(["jevscreen", "sieve", "pin", match.security_id, want, "--run", ctx.ref.run_id],
                          ask_human=True),
                    _from_run(ctx, cost=0.0 if read else l2u, edit=True)],   # a pinned company is read by step 2
                   "它会进名单，标为「用户判断」", "It is listed, marked as your judgement")


def _sync_change(ctx: RunCtx, match, l2u: float) -> list[dict[str, Any]]:
    from . import keys
    exch, _, code = (match.security_id or "").partition(":")
    spec = SYNC_BY_EXCHANGE.get(exch)
    if spec is None:
        if exch in US_EXCHANGES:
            zh, en = ("美股的年报原文只能整体同步（sync-sec 不能按公司下载）",
                      "US annual reports come only from a full sync (sync-sec has no --codes)")
        else:
            zh, en = (f"这个市场（{exch}）还没有下载年报原文的命令", f"No annual-report download for this market ({exch}) yet")
        return [_change("gap_no_sync", zh, en, [], "只能以后整体同步时补上" if exch in US_EXCHANGES else "这是数据缺口",
                        "Only a full sync later can add it" if exch in US_EXCHANGES else "This is a data gap")]
    cmd, key, secs, flags = spec
    if key is not None:
        ok = ctx.cfg is not None and bool(keys.presence(ctx.cfg, key).get("configured"))
        if not ok:
            return [_change("gap_key", f"要补它的年报需要 {key} 钥匙（还没有设置）",
                            f"Its annual report needs the {key} key (not configured)", [],
                            "有了钥匙才能下载", "Needs the key first")]
    return [_change("sync_one", f"下载它的年报原文（{cmd}，约 {secs} 秒），再从这次运行重跑第二步",
                    f"Download its annual report ({cmd}, ~{secs} s), then rerun step 2 from this run",
                    [_step(["jevscreen", cmd, *flags, "--codes", code], network=True, seconds=secs),
                     _from_run(ctx, cost=l2u)],
                    "第二步会读年报原文而不是简介", "Step 2 then reads the annual report instead of the profile")]


def _l2_not_sent(ctx: RunCtx, out: dict[str, Any], match) -> dict[str, Any]:
    _, l2u = units(ctx.result)
    gaps = ctx.result.get("gaps") or {}
    over = [g["security_id"] for g in gaps.get("l2_not_sent_l2_max") or []]
    l2_max = ctx.params.get("l2_max") or 0
    _set(out, "l2_not_sent", "第一步通过了，但第二步只读前面的公司（--l2-max），它排在后面没读到",
         "It passed step 1, but step 2 reads only the first --l2-max companies and it was beyond that")
    out["stages"].append({"id": "l2", "ok": False, "text_zh": "第二步：没读（超出 --l2-max）",
                          "text_en": "step 2: not read (beyond --l2-max)"})
    pos = over.index(match.security_id) + 1 if match.security_id in over else len(over) or 1
    n = l2_max + pos
    out["changes"].append(_change("raise_l2_max", f"把 --l2-max 提到 {n}，从这次运行重跑（第一步免费）",
                                  f"Raise --l2-max to {n} and rerun from this run (step 1 free)",
                                  [_from_run(ctx, "--l2-max", str(n), cost=pos * l2u)],
                                  "第二步会读到它", "Step 2 reads it"))
    return out


def _below_cut(ctx: RunCtx, out: dict[str, Any], match) -> dict[str, Any]:
    from . import calib
    max_out = int(ctx.params.get("max_out") or 40)
    rank = None
    if ctx.pool is not None:      # the full ranking, offline (the same code as screen's step 4)
        verified, unverified = calib.ranking_entries(calib._merge_candidates(ctx.result, ctx.pool), ctx.sieve,
                                                     ctx.params.get("rank") or "label")
        v2, _u2, _ex, _n = screen.pin_and_rank(verified, unverified, ctx.sieve, max_out)
        rank = next((i for i, e in enumerate(v2, 1) if e["company_key"] == match.company_key), None)
    _set(out, "ranked_below_cut", f"它通过了两步，但排第 {rank or '?'}，名单只显示前 {max_out}",
         f"It passed both steps but ranks #{rank or '?'}; the list shows the top {max_out}")
    out["stages"].append({"id": "rank", "ok": False, "text_zh": f"名单：第 {rank or '?'}（前 {max_out} 才显示）",
                          "text_en": f"list: #{rank or '?'} (top {max_out} shown)"})
    if rank:
        out["changes"].append(_change("raise_max_out", f"让名单显示前 {rank} 家（从这次运行重排，已读的免费复用）",
                                      f"Show the top {rank} (rerun from this run; everything read is reused)",
                                      [_from_run(ctx, "--max-out", str(rank), cost=0.0)],
                                      "它会出现在名单里", "It appears in the list"))
    return out


def _in_output(ctx: RunCtx, out: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    vs = row.get("verdict_source")
    lab = row.get("l2_label")
    extra_zh = "（你钉选的）" if vs == "user" else "（证据和你的判断一致）" if vs == "evidence+user" else ""
    _set(out, "in_output", f"它在名单里，排第 {row['rank']}{extra_zh}",
         f"It is in the list at #{row['rank']}" + (" (your pin)" if vs == "user" else ""))
    out["stages"].append({"id": "rank", "ok": True, "text_zh": f"名单：第 {row['rank']}，第二步「{LABEL_ZH.get(lab, lab)}」",
                          "text_en": f"list: #{row['rank']}, step 2 \"{_lab_en(lab)}\"",
                          "data": {"rank": row["rank"], "l2_label": lab, "verdict_source": vs,
                                   "backfill": row.get("backfill"), "edge": row.get("l2_edge")}})
    if row.get("backfill"):
        out["facts"].append({"zh": "递补，未经确认：别人被你排除后它才进前面", "en": "backfill: moved up after your exclusions"})
    if row.get("l2_edge"):
        out["inferences"].append({"zh": "边缘：再读一次可能会翻", "en": "borderline: another read could flip it"})
    return out


def _excluded(ctx: RunCtx, out: dict[str, Any], row: dict[str, Any], match) -> dict[str, Any]:
    _set(out, "excluded_by_user", "你（或你让 AI）把它标为「不要」，所以不排名，证据照列",
         "You marked it 'no', so it is not ranked (its evidence is still listed)")
    exs = [ex for ex in (ctx.sieve or {}).get("examples") or [] if not ex.get("_author")]
    n = next((i for i, ex in enumerate(exs, 1) if ex.get("pin") and ex.get("want") == "no"
              and (ex.get("company_key") == match.company_key or ex.get("security_id") == match.security_id)), None)
    ex = exs[n - 1] if n else {}
    if row.get("l2_label") or row.get("l2_status"):
        lab = row.get("l2_label") or row.get("l2_status")
        out["stages"].append({"id": "l2", "ok": lab in ("explicit", "partial"), "text_zh": f"第二步：{_lab_zh(lab)}",
                              "text_en": f"step 2: {_lab_en(lab)}"})
    if ex.get("via") == "pin":
        out["facts"].append({"zh": f"来源：你让 AI 钉选的（{str(ex.get('at') or '')[:10]}）", "en": "source: sieve pin"})
        step = _step(["jevscreen", "sieve", "unpin", match.security_id, "--run", ctx.ref.run_id], ask_human=True)
    else:
        out["facts"].append({"zh": f"来源：你答卡片时选的（{ex.get('deck_id') or '-'}）", "en": "source: card answer"})
        step = _step(["jevscreen", "answer", "--undo", str(n or 0), "--run", ctx.ref.run_id, "--no-apply"],
                     ask_human=True)
    out["changes"].append(_change("undo_no", "撤销这个「不要」（需要你本人同意）", "Undo this 'no' (needs your own yes)",
                                  [step], "下次运行它会按证据排名", "It is ranked by evidence again"))
    return out


# ---------------------------------------------------------------------------------------------------- rendering

NAMES_EN = {"universe": "universe", "shells": "shells", "input": "text", "l1": "step 1", "l2": "step 2",
            "rank": "list"}


def render_text(exp: dict[str, Any], lang: str = "zh", *, verbose: bool = False) -> str:
    """The human text of one explanation: header, one plain sentence, numbered stages, facts / inference / gaps,
    what would change it."""
    zh = lang == "zh"
    run = exp["run"]
    idea = (run.get("idea") or "")
    idea_s = idea[:30] + ("…" if len(idea) > 30 else "")
    date = str(run.get("started_at") or "")[:10]
    who = exp.get("who_zh" if zh else "who_en") or exp["target"]
    lines = [f"{who} · 你的想法「{idea_s}」· {date} 的筛选" if zh else f"{who} · idea \"{idea_s}\" · screen of {date}"]
    if run.get("dry_run"):
        lines.append("这是试算，第一层还没问模型" if zh else "This was a dry run: step 1 has not asked the model yet")
    if exp.get("status") == "partial_files_only":
        lines.append("筛选正在运行（数据库被占用）：先按运行记录回答，第二步的细节稍后再问。" if zh else
                     "The store is busy: answered from the run's files; ask again later for step-2 details.")
    if verbose:
        lines.append(f"run {run['run_id']}")
    lines.append(("一句话：" if zh else "One line: ") + (exp.get("plain_zh" if zh else "plain_en") or ""))
    names_zh = {"universe": "股票池", "shells": "排除壳公司", "input": "资料", "l1": "第一步", "l2": "第二步",
                "rank": "名单"}
    for i, s in enumerate(exp.get("stages") or [], 1):
        mark = "✓" if s.get("ok") else ("—" if s.get("ok") is None else "✗")
        text = s.get("text_zh" if zh else "text_en") or ""
        text = text.split("：", 1)[-1] if zh and "：" in text else text.split(": ", 1)[-1]
        label = names_zh.get(s["id"], s["id"]) if zh else NAMES_EN.get(s["id"], s["id"])
        lines.append(f" {i} {label:<6} {mark} {text}")
        rule = s.get("rule_zh" if zh else "rule_en")
        if rule and not s.get("ok"):
            lines.append(f"               {rule}")
    for tag_zh, tag_en, key in (("[事实]", "[fact]", "facts"), ("[推断]", "[inference]", "inferences"),
                                ("[缺口]", "[gap]", "gaps")):
        for f in exp.get(key) or []:
            lines.append(f"{tag_zh if zh else tag_en} {f.get('zh' if zh else 'en')}")
    if exp.get("changes"):
        lines.append("怎样会改变：" if zh else "What would change it:")
        for c in exp["changes"]:
            cost = c["cost_text_zh" if zh else "cost_text_en"]
            secs = f"，约 {max(1, round(c['seconds'] / 60))} 分钟" if (zh and c["seconds"]) else (
                f", ~{max(1, round(c['seconds'] / 60))} min" if c["seconds"] else "")
            if not c.get("steps"):          # a gap or a hint, not an action: no price
                lines.append(f"  · {c['text_zh' if zh else 'text_en']}")
            else:
                lines.append(f"  · {c['text_zh' if zh else 'text_en']}（{cost}{secs}）" if zh else
                             f"  · {c['text_en']} ({cost}{secs})")
            lines.append(f"    {c['outcome_zh' if zh else 'outcome_en']}")
            if c["ask_human"]:
                lines.append("    （需要你同意）" if zh else "    (needs your yes)")
    return "\n".join(lines)


def lang_of(idea: str | None, lang: str = "auto") -> str:
    if lang in ("zh", "en"):
        return lang
    return "zh" if idea and (screen.script_counts(idea)["han"] + screen.script_counts(idea)["kana"]
                             + screen.script_counts(idea)["hangul"]) else "en"


def check_targets(ctx: RunCtx) -> list[str]:
    """--checks: every company the run's sieve names (should_pass, should_fail, pins), as security ids."""
    out = []
    for ex in (ctx.sieve or {}).get("examples") or []:
        if ex.get("source") == "sieve" or ex.get("pin"):
            t = ex.get("security_id") or ex.get("company_key")
            if t and t not in out:
                out.append(t)
    return out


# ------------------------------------------------------------------------------------------------------ entry

def run(cfg, targets: list[str], *, run_ref: str | None = None, idea: str | None = None, key: str | None = None,
        checks: bool = False, wait_s: float = WAIT_S, show_text: bool = False) -> dict[str, Any]:
    """{'command': 'why', 'status', 'run', 'results': [explanation], 'sources_used'}; raises WhyError when no run can
    be chosen. The store is opened read-only for at most wait_s seconds; when it stays locked the answers come from
    the run's files (status 'partial_files_only')."""
    from . import store
    if cfg.db_path.exists():
        try:
            with store.session(cfg, read_only=True, wait_s=wait_s) as con:
                return _run(cfg, con, targets, run_ref, idea, key, checks, show_text)
        except store.StoreLocked:
            pass
    return _run(cfg, None, targets, run_ref, idea, key, checks, show_text)


def _run(cfg, con, targets, run_ref, idea, key, checks, show_text) -> dict[str, Any]:
    ctx = load_ctx(cfg, pick_run(list_runs(cfg, con), run_ref, idea=idea, key=key), con)
    ref = ctx.ref
    ts = list(targets or []) + (check_targets(ctx) if checks else [])
    results = []
    for t in dict.fromkeys(ts):
        results.append(explain(ctx, resolve_target(ctx, t), show_text=show_text))
    status = ctx.status
    if any(r["stop"] in ("not_found", "ambiguous") for r in results):
        status = "not_found" if all(r["stop"] == "not_found" for r in results) else "partly_resolved"
    out = {"command": "why", "status": status, "run": {"run_id": ref.run_id, "idea": ctx.result.get("idea"),
                                                       "started_at": ctx.result.get("started_at"),
                                                       "status": ctx.result.get("status"),
                                                       "dry_run": bool(ctx.result.get("dry_run")),
                                                       "output_dir": str(ref.out_dir) if ref.out_dir else None},
           "results": results, "sources_used": (ctx.index.sources_used if ctx.index else ["ledger"]),
           "files_only": ctx.status == "partial_files_only"}
    notes_zh, notes_en = [], []
    if ctx.requested:
        out["run"]["requested_run_id"] = ctx.requested
        notes_zh.append(f"运行 {ctx.requested} 已被 {ref.run_id}（补抓年报后的更新）替换，同一个目录里的结果和记录都是新的："
                        f"下面解释的是 {ref.run_id}")
        notes_en.append(f"Run {ctx.requested} was updated by {ref.run_id} (after fetching annual reports), which "
                        f"replaced its files in the same folder: explaining {ref.run_id}")
    if ctx.sieve_changed:
        notes_zh.append("筛子在这次运行之后改过：说明按运行时的结果，改动下次运行才生效")
        notes_en.append("The sieve changed after this run: the explanation reflects the run; changes apply next run")
    if notes_zh:
        out["note_zh"], out["note_en"] = "；".join(notes_zh), " ".join(n + "." for n in notes_en)[:-1]
    if not results:
        out["status"] = "no_targets"
    return out
