"""The sand panning (淘沙) of the one page: a full-window 3D scene behind a restrained HUD (jevscreen.page_hud), the
page itself while JavaScript runs (owner story, decided 2026-09-28: 立体感, 变化的快速感, 淘沙; then 2026-09-28 late:
one scene, the sieve stack dominant, a precision instrument rather than an arcade).

The story: on the first view the camera flies into a star sea (every listed company) and the stars turn into fine
sand resting on the first of three sieves floating in space. Each sieve is one screening stage, top to bottom: the
market-cap floor, the AI's first read of the profiles, the annual-report check. The sieves are square meshes seen in
true perspective (a camera orbits slowly, near grains are larger and brighter, far ones smaller and dimmer, lower
sieves sit deeper; each sieve's plane hides part of what lies below it). Each sieve carries its own progress as a
stroke that fills around its rim (a faint track, the done share bright, a short arc circling while the amount is
unknown, the whole rim once done). When a stage completes, its sieve gives one short shake, its rim closes, its real
numbers roll up beside it for ~2.6 s and fade, and the sand that passes rushes down to the next sieve in about 0.6 s.
At the end a few amber grains (the final list, the only accent colour) stay on the last sieve, joined by one thin
amber line. No label column, no leader lines, no permanent words: a sieve tells its stage and numbers on hover or
keyboard focus (tips()), an amber grain its rank and company; the results panel and the grains light each other.

Only REAL numbers are drawn: facts() reads them from the quickstart job and the result funnel (pagestatus.build puts
them in data['live']['sand']); markup() writes them, and the final list's short names, as data attributes of one
<div id="sand"> (hidden without JavaScript and in the text view). The grains on each sieve are a compressed share of
the real counts (at most 1,600 grains; the counts themselves are in the tooltips and the text view). Everything is
inline (the page's CSP: no network, no assets, no library: the 3D projection is a few lines of canvas 2D).
The page reloads every 3 s while work runs (never within ~4 s of someone using the scene: page.REFRESH_JS): the
animation phase, the last stage seen, whether the opening was seen and the view (orbit, zoom, pan) live in
sessionStorage, so the motion continues across reloads, the opening plays once, and each stage's shake plays once
(only for a live transition: the last page of this key was left seconds ago in this tab). The first drawing is made
at once on load; then requestAnimationFrame at about 30 fps (60 during the opening, a rush and while someone plays),
stopped while the tab is hidden or the text view is shown; prefers-reduced-motion gets one still drawing with the
current numbers, redrawn only for a tooltip or a view the user changes (no inertia, no flight: the view jumps).

Model-viewer controls (owner, 2026-09-28): the wheel or a pinch zooms towards the pointer, a drag orbits (inertia,
soft limits), a right-drag, a middle-drag, a shift-drag or a two-finger drag pans, a double-click (a double tap) on a
sieve flies the camera to it and elsewhere resets the view; keys on the focused scene: arrows up/down step through
the sieves and the amber grains, Enter flies to a sieve or opens a grain's row, arrows left/right orbit, + and -
zoom, 0 or Esc resets. The camera's math is pure (CAM: node tests run it alone). The mouse still tilts the scene a
little, the pointer is a force field that moves the sand (CORE, a small pure physics), a click sends a shockwave.
The README hero GIFs (tools/make_hero.py) are not interactive.
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import re
from typing import Any

# label index: 0 every listed company, 1 the market-cap sieve, 2 the first read, 3 the annual-report check, 4 the list
LAYERS = 5
FINAL_DOTS = 10          # amber grains on the last sieve: the top of the list (the page's top table)

LABELS = {
    "zh": {"all": "全部上市公司", "pool": "市值 ≥ {floor}", "read": "AI 初读 · 有简介", "passed": "初读通过",
           "profiles": "有简介", "check": "年报核对", "final": "入选", "aria": "筛选：{steps}",
           "aria_none": "筛选",
           # the tooltip of a sieve (hover, tap or keyboard focus): its stage and its real numbers
           "tip_s0": "市值 ≥ {floor}", "tip_s1": "AI 初读", "tip_s2": "年报核对",
           "tip_of": "：{a} / {b} 家", "tip_n": "：{a} 家", "tip_pass": "：通过 {a} / {b} 家",
           "tip_read": "：已读 {a} / {b} 家", "tip_keep": "：入选 {a} / {b} 家", "tip_checked": "：已核对 {a} / {b} 家",
           "tip_queue": "：待读 {a} 家", "tip_wait": "：还没开始",
           # an amber grain's tooltip: how to open its evidence (a mouse click; a second tap on a phone)
           "tip_click": "点击看证据", "tip_again": "再点一次看证据"},
    "en": {"all": "ALL LISTED", "pool": "MCAP ≥ {floor}", "read": "FIRST READ · WITH PROFILE", "passed": "PASSED READ",
           "profiles": "WITH PROFILE", "check": "REPORT CHECK", "final": "FINAL LIST", "aria": "Screening: {steps}",
           "aria_none": "Screening",
           "tip_s0": "Market cap ≥ {floor}", "tip_s1": "First read", "tip_s2": "Report check",
           "tip_of": ": {a} of {b} companies", "tip_n": ": {a} companies", "tip_pass": ": {a} of {b} passed",
           "tip_read": ": {a} of {b} read", "tip_keep": ": {a} of {b} made the list", "tip_checked": ": {a} of {b} checked",
           "tip_queue": ": {a} waiting", "tip_wait": ": not started yet",
           "tip_click": "click for evidence", "tip_again": "tap again for evidence"},
}


def _int(v: Any) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _steps(job: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for s in (job or {}).get("steps") or []:
        if isinstance(s, dict) and s.get("id"):
            out[s["id"]] = s
    return out


def _ok(s: dict[str, Any] | None) -> bool:
    return bool(s) and s.get("status") in ("ok", "skipped")


def facts(job: dict[str, Any] | None, result: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The raw numbers of the drawing from a quickstart job (and a page result's funnel): None when there is
    neither. `stage` is how deep the work got (0 nothing, 1 stock list, 2 profiles, 3 first read, 4 final list),
    `active` the layer now working (None when nothing runs) with `frac` its share done (None: unknown)."""
    if not job and not result:
        return None
    st = _steps(job)
    p = (job or {}).get("progress") or {}
    stages = p.get("stages") if isinstance(p.get("stages"), dict) else {}
    fn = (result or {}).get("funnel") or {}
    u, d = st.get("universe"), st.get("descriptions")
    l1, l2 = stages.get("l1") or {}, stages.get("l2") or {}
    jst = (job or {}).get("state")
    result_ok = (result or {}).get("status") in ("ok", "partial", "budget_exhausted")
    screen_done = _ok(st.get("screen")) or result_ok
    l1_done, l1_total = _int(l1.get("done")), _int(l1.get("total"))
    l1_full = l1_total is not None and l1_done is not None and l1_total > 0 and l1_done >= l1_total
    if screen_done:
        stage = 4
    elif l1_full and (p.get("stage") in ("l2", "fetch") or bool(l2)):
        stage = 3
    elif _ok(d):
        stage = 2
    elif _ok(u):
        stage = 1
    else:
        stage = 0
    active, frac = None, None
    if jst == "running" and not screen_done:
        cur = p.get("stage")
        g = {"universe": None, "descriptions": stages.get("descriptions") or {}, "l1": l1, "l2": l2,
             "fetch": l2}.get(cur, None) if cur in ("universe", "descriptions", "l1", "l2", "fetch") else None
        active = {"universe": 0, "descriptions": 1, "l1": 2, "l2": 3, "fetch": 3}.get(cur)
        if g:
            dn, tt = g.get("done"), g.get("total")
            if isinstance(dn, (int, float)) and isinstance(tt, (int, float)) and tt > 0:
                frac = round(max(0.0, min(1.0, float(dn) / float(tt))), 3)
    state = {"running": "run", "waiting": "wait", "new": "wait", "failed": "fail", "interrupted": "fail",
             "declined": "fail", "done": "done"}.get(jst or "", "done" if result_ok else "wait")
    if screen_done and state != "fail":
        state = "done" if jst in (None, "done") or result_ok else state
    return {
        "stage": stage, "active": active, "frac": frac, "state": state,
        "listed": _int((u or {}).get("listed")),
        "pool": _int(fn.get("universe")) if fn.get("universe") is not None else _int((u or {}).get("companies")),
        "profiles": _int(fn.get("described")) if fn.get("described") is not None else _int((d or {}).get("described")),
        "l1_done": l1_done, "l1_total": l1_total, "l1_pass": _int(fn.get("l1")),
        "l2_done": _int(l2.get("done")), "l2_total": _int(l2.get("total")),
        "final": _int(fn.get("listed")),
        "floor": (job or {}).get("min_mcap_usd") or fn.get("floor"),
    }


def layer_view(f: dict[str, Any], lang: str) -> list[dict[str, Any]]:
    """One {label, a, b} per layer in `lang` ('a / b' when b is set; no number when a is None): the canvas's
    accessible name (aria())."""
    from . import l10n
    lang = "en" if lang == "en" else "zh"
    L = LABELS[lang]
    floor = l10n.usd_words(float(f.get("floor") or 1e9), lang)
    stage = f.get("stage") or 0
    r0 = {"label": L["all"], "a": f.get("listed"), "b": None}
    r1 = {"label": L["pool"].format(floor=floor), "a": f.get("pool"), "b": None}
    if stage >= 4 and f.get("l1_pass") is not None:
        r2 = {"label": L["passed"], "a": f.get("l1_pass"), "b": None}
    elif f.get("l1_total") is not None:
        r2 = {"label": L["read"], "a": f.get("l1_done") or 0, "b": f.get("l1_total")}
    elif f.get("profiles") is not None:
        r2 = {"label": L["profiles"], "a": f.get("profiles"), "b": None}
    else:
        r2 = {"label": L["read"], "a": None, "b": None}
    if f.get("l2_total") is not None:
        r3 = {"label": L["check"], "a": f.get("l2_done") or 0, "b": f.get("l2_total")}
    elif stage >= 4 and f.get("l1_pass") is not None:
        # a finished list without live report-check progress: what was checked is what passed the first read
        r3 = {"label": L["check"], "a": f.get("l1_pass"), "b": None}
    else:
        r3 = {"label": L["check"], "a": None, "b": None}
    r4 = {"label": L["final"], "a": f.get("final") if stage >= 4 else None, "b": None}
    return [r0, r1, r2, r3, r4]


def readings(f: dict[str, Any]) -> list[tuple[str | None, int | None, int | None]]:
    """What each sieve (top to bottom) reads now: (tooltip key, a, b); (None, None, None) when it has not started.
    The tooltips (tips()) and the numbers a completed stage shows beside its rim (nums()) both come from here."""
    stage = f.get("stage") or 0
    passed = f.get("l1_pass") if f.get("l1_pass") is not None else (f.get("l2_total") if stage >= 3 else None)
    read_of = next((v for v in (f.get("l1_total"), f.get("profiles"), f.get("pool")) if v is not None), None)
    r0 = ("tip_of", f.get("pool"), f.get("listed")) if f.get("pool") is not None else (None, None, None)
    if passed is not None and stage >= 3:
        r1 = ("tip_pass", passed, read_of)
    elif f.get("l1_total") is not None:
        r1 = ("tip_read", f.get("l1_done") or 0, f.get("l1_total"))
    elif stage >= 1 and read_of is not None:     # the sand waits on this sieve: what the first read will read
        r1 = ("tip_queue", read_of, None)
    else:
        r1 = (None, None, None)
    if stage >= 4 and f.get("final") is not None:
        r2 = ("tip_keep", f.get("final"), passed)
    elif f.get("l2_total") is not None:
        r2 = ("tip_checked", f.get("l2_done") or 0, f.get("l2_total"))
    else:
        r2 = (None, None, None)
    return [(k, _int(a), _int(b)) for k, a, b in (r0, r1, r2)]


def tips(f: dict[str, Any], lang: str) -> list[str]:
    """The three sieves' tooltips (top to bottom) with their REAL numbers, e.g. 'AI 初读：通过 296 / 10,261 家'."""
    from . import l10n
    lang = "en" if lang == "en" else "zh"
    L = LABELS[lang]
    heads = [L["tip_s0"].format(floor=l10n.usd_words(float(f.get("floor") or 1e9), lang)), L["tip_s1"], L["tip_s2"]]
    out = []
    for head, (key, a, b) in zip(heads, readings(f)):
        if key is None or a is None:
            out.append(head + L["tip_wait"])
            continue
        if b is None and key in ("tip_of", "tip_pass", "tip_keep"):
            key = "tip_n"
        out.append(head + L[key].format(a=f"{a:,}", b=f"{b:,}" if b is not None else ""))
    return out


def nums(f: dict[str, Any]) -> list[str]:
    """Each sieve's numbers as 'a,b' (b may be empty; '' before the stage starts): drawn beside its rim, numbers only,
    for ~2.6 s when its stage completes."""
    return ["" if a is None else f"{a},{'' if b is None else b}" for _, a, b in readings(f)]


def counts(f: dict[str, Any]) -> list[int | None]:
    """The four counts that size the sand: every listed company, the market-cap pool, what passed the first read
    (the report check's intake while it runs), the final list."""
    passed = f.get("l1_pass") if f.get("l1_pass") is not None else f.get("l2_total")
    return [_int(f.get("listed")), _int(f.get("pool")), _int(passed),
            _int(f.get("final")) if (f.get("stage") or 0) >= 4 else None]


_SUFFIX = re.compile(r"(?:[\s,]+(?:co\.?,?\s*ltd\.?|company\s+limited|limited|ltd\.?|inc\.?|incorporated|corp\.?|"
                     r"corporation|plc|class\s+[a-z]|s\.a\.?|ag|n\.v\.?|co\.?))+$", re.I)


def short_name(name: str) -> str:
    """A company's short name for a label: the legal suffixes of a Latin name dropped ('Baifeng Aluminium Co. Ltd.
    Class A' -> 'Baifeng Aluminium'); a CJK short name unchanged."""
    s = " ".join(str(name or "").split())
    t = _SUFFIX.sub("", s).strip(" ,")
    return t or s


def picks(data: dict[str, Any], n: int) -> list[list[Any]]:
    """[short name, ticker, rank] of the top n rows of the main list (with the sections, the confirmed list only: the
    to-confirm section is never gold), the name a reader of the page's language recognises (the rank finds the row's
    evidence when a grain is clicked). On a Chinese page a company without a Chinese short name gets an empty name
    (the drawing shows its ticker alone, never an English name among Chinese ones)."""
    from .page import display_name, main_and_confirm
    lang = "en" if data.get("lang") == "en" else "zh"
    out = []
    for r in main_and_confirm(data)[0][:n]:
        if isinstance(r, dict):
            nm = short_name(display_name(r, lang))[:40]
            if lang == "zh" and not re.search(r"[㐀-鿿]", nm):
                nm = ""
            rk = r.get("rank")
            out.append([nm, str(r.get("ticker") or "")[:16], int(rk) if isinstance(rk, int) else None])
    return out


# ---- the reading log (owner, 2026-09-29): a faint monospace column that types the run's real recent per-company
# answers ("300990 · 相关 · 读年报" / "OKTA · core · profile"): the code, the answer, then the stage, so the answer
# still shows when the column is narrow (about 190 px beside the open results panel: motion check 2026-09-29; the
# stage may fade under the right edge). Only a security id of a strict shape, a fixed label word and a fixed stage
# word ever reach the page: never a name, a text, a path or anything else. The label words are the results panel's
# (explicit: 写明 / stated, the confirmed list's 原文写明 / Stated).
LOG_MAX = 60
LOG_WORDS = {
    "zh": {"l1": "读简介", "l2_annual_report": "读年报", "l2_profile": "复读简介", "l2": "核对",
           "core": "核心", "adjacent": "相邻", "unrelated": "无关", "insufficient": "说不清",
           "explicit": "写明", "partial": "相关", "contradicted": "不符"},
    # English label words of different shapes (never a pair like clear / unclear that a prefix tells apart), in the
    # project's own vocabulary
    "en": {"l1": "profile", "l2_annual_report": "report", "l2_profile": "reread", "l2": "check",
           "core": "core", "adjacent": "adjacent", "unrelated": "off-topic", "insufficient": "thin",
           "explicit": "stated", "partial": "related", "contradicted": "no fit"},
}
_SID = re.compile(r"^[A-Z][A-Z0-9]{1,9}:[A-Za-z0-9][A-Za-z0-9.\-]{0,11}$")    # EXCHANGE:CODE only


def log_ticker(sid: Any) -> str | None:
    """A security id as the log shows it: the code alone, without its exchange ('SZSE:300990' -> '300990',
    'NASDAQ:OKTA' -> 'OKTA': the column is narrow); None for anything that is not a plain EXCHANGE:CODE security
    id."""
    s = str(sid or "").strip()
    if not _SID.match(s):
        return None
    return s.split(":", 1)[-1]


def log_line(ev: Any, lang: str) -> str | None:
    """One event of the job ({layer, sid, label, doc}) as a line of the log in `lang`; None when any part is not
    one of the known words (nothing unknown is ever shown)."""
    if not isinstance(ev, dict):
        return None
    L = LOG_WORDS["en" if lang == "en" else "zh"]
    tk = log_ticker(ev.get("sid") or ev.get("security_id"))
    layer, label = ev.get("layer"), ev.get("label")
    if tk is None or layer not in ("l1", "l2") or label not in L or label in ("l1", "l2"):
        return None
    if layer == "l1" and label not in ("core", "adjacent", "unrelated", "insufficient"):
        return None
    if layer == "l2" and label not in ("explicit", "partial", "contradicted", "insufficient"):
        return None
    stage = L["l1"] if layer == "l1" else L.get(f"l2_{ev.get('doc')}", L["l2"])
    return f"{tk} · {L[label]} · {stage}"


def log_lines(job: dict[str, Any] | None, lang: str) -> list[list[Any]]:
    """The reading log's lines from a quickstart job: [[n, line], ...] oldest first, at most LOG_MAX (the job keeps
    the most recent answers, each with a sequence number that only grows: the page types the ones it has not)."""
    evs = (((job or {}).get("progress") or {}).get("events")) or []
    out: list[list[Any]] = []
    for e in evs[-LOG_MAX:] if isinstance(evs, list) else []:
        n = e.get("n") if isinstance(e, dict) else None
        line = log_line(e, lang)
        if isinstance(n, int) and not isinstance(n, bool) and line:
            out.append([n, line])
    return out


def log_src(job: dict[str, Any] | None) -> str:
    """Where the log's sequence numbers come from: this job ('j' and a short hash of when it was made: a job made
    again for the same idea starts its numbers again) or '' (none: the listed rows). The page starts counting again
    when it changes."""
    c = str((job or {}).get("created_at") or "")
    return ("j" + hashlib.sha1(c.encode("utf-8")).hexdigest()[:8]) if job else ""


def log_from_rows(data: dict[str, Any]) -> list[list[Any]]:
    """A page without the job's events (a plain screen's page, an old job): the listed rows' own check answers."""
    lang = "en" if data.get("lang") == "en" else "zh"
    out: list[list[Any]] = []
    for r in (data.get("rows") or [])[:LOG_MAX]:
        if not isinstance(r, dict):
            continue
        det = r.get("details") if isinstance(r.get("details"), dict) else {}
        line = log_line({"layer": "l2", "sid": r.get("security_id"), "label": det.get("l2"),
                         "doc": r.get("evidence")}, lang)
        if line:
            out.append([len(out) + 1, line])
    return out


def _num(v: Any) -> str:
    return "" if v is None else str(int(v))


def aria(views: list[dict[str, Any]], lang: str) -> str:
    L = LABELS["en" if lang == "en" else "zh"]
    parts = []
    for r in views:
        if r["a"] is None:
            continue
        n = f"{int(r['a']):,}" + (f" / {int(r['b']):,}" if r.get("b") is not None else "")
        parts.append(f"{r['label']} {n}")
    return L["aria"].format(steps=" → ".join(parts)) if parts else L["aria_none"]


def markup(data: dict[str, Any]) -> str:
    """The scene's element for the page (empty when the page has no job and no result): numbers and the final
    list's short names as data attributes; a focusable canvas whose accessible name lists the real numbers."""
    live = data.get("live") or {}
    f = live.get("sand")
    if not isinstance(f, dict):
        return ""
    lang = "en" if data.get("lang") == "en" else "zh"
    views = layer_view(f, lang)
    stage = int(f.get("stage") or 0)
    final = f.get("final") if stage >= 4 else None
    from .page import main_and_confirm
    main = main_and_confirm(data)[0]         # the gold is the main list only (never the to-confirm section)
    top = min(FINAL_DOTS, int(data.get("top_n") or FINAL_DOTS),
              len(main) if data.get("shortlist") else (len(main) or (final or 0)))
    dots = top if final else 0
    attrs = {"key": data.get("idea_key") or "", "lang": lang, "state": f.get("state") or "wait",
             "stage": str(stage), "active": _num(f.get("active")),
             "frac": "" if f.get("frac") is None else f"{float(f['frac']):.3f}",
             "dots": str(dots), "cnt": ",".join(_num(v) for v in counts(f)),
             "picks": json.dumps(picks(data, dots), ensure_ascii=False) if dots else "[]",
             "tip-click": LABELS[lang]["tip_click"], "tip-again": LABELS[lang]["tip_again"]}
    stale = live.get("stale") or {}
    if stale.get("since"):          # the page notices a dead worker (see page.JS): no stage works, the drawing idles
        attrs.update({"stale-since": stale["since"], "stale-after": str(int(stale.get("after_s") or 0))})
    for k, t in enumerate(tips(f, lang)):
        attrs[f"tip{k}"] = t
    for k, n in enumerate(nums(f)):
        attrs[f"n{k}"] = n
    log = [e for e in (live.get("log") or []) if isinstance(e, list) and len(e) == 2][-LOG_MAX:]
    src = str(live.get("log_src") or "")[:12] if log else "r"
    attrs["log"] = json.dumps(log or log_from_rows(data), ensure_ascii=False)
    attrs["log-from"] = src
    a = " ".join(f'data-{k}="{_html.escape(str(v), quote=True)}"' for k, v in attrs.items())
    label = _html.escape(aria(views, lang), quote=True)
    return (f'<div id="sand" class="sand" {a}><canvas role="img" tabindex="0" aria-label="{label}" width="1" '
            f'height="1"></canvas><div class="sandlog" aria-hidden="true"></div></div>\n')


CSS = """
.sand{display:none;position:fixed;left:0;top:0;right:0;bottom:0;z-index:0;background:#070707;overflow:hidden}
html.hud-on .sand.on{display:block}html.hud-text .sand,html.hud-text .sand.on{display:none}
.sand canvas{display:block;width:100%;height:100%;touch-action:none;-webkit-user-select:none;user-select:none;
-webkit-tap-highlight-color:transparent;outline:none}
.sand canvas:focus-visible{box-shadow:inset 0 0 0 1px rgba(242,237,224,.2)}
.sandtip{position:fixed;left:0;top:0;z-index:40;pointer-events:none;max-width:min(380px,calc(100vw - 32px));
padding:4px 8px;border-radius:5px;background:rgba(7,7,7,.82);border:1px solid rgba(242,237,224,.1);
color:rgba(242,237,224,.8);font:11px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,"PingFang SC",
"Hiragino Sans GB","Microsoft YaHei",monospace;letter-spacing:.06em;white-space:nowrap;overflow:hidden;
text-overflow:ellipsis;opacity:0;transform:translateY(2px);transition:opacity .16s ease,transform .16s ease}
.sandtip.on{opacity:1;transform:none}.sandtip[data-hint]::after{content:attr(data-hint);margin-left:10px;
color:rgba(242,237,224,.42)}
details.row.sandlit{box-shadow:0 0 0 2px rgba(239,159,39,.5);transition:box-shadow .8s ease}
details.row.sandhov,tr.sandhov td{box-shadow:inset 3px 0 0 rgba(239,159,39,.55)}
@media (prefers-reduced-motion:reduce){.sandtip,details.row.sandlit{transition:none;animation:none}}
.sandlog{position:absolute;left:16px;bottom:calc(46px + var(--hud-pb,0px));width:min(300px,26vw);
height:min(34vh,250px);display:flex;flex-direction:column;justify-content:flex-end;overflow:hidden;
pointer-events:none;-webkit-user-select:none;user-select:none;opacity:.36;color:#f2ede0;
font:11px/1.8 ui-monospace,SFMono-Regular,Menlo,Consolas,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",monospace;
letter-spacing:.02em;white-space:nowrap;font-variant-numeric:tabular-nums;contain:layout paint;
transition:bottom .35s ease;
-webkit-mask-image:linear-gradient(to bottom,transparent,#000 60%),linear-gradient(to right,#000 72%,transparent);
-webkit-mask-composite:source-in;
mask-image:linear-gradient(to bottom,transparent,#000 60%),linear-gradient(to right,#000 72%,transparent);
mask-composite:intersect}
.sandlog>div{overflow:hidden;opacity:.78}.sandlog>div.cur{opacity:1}
@media (max-width:759px){.sandlog{display:none}}
@media print{.sandlog{display:none}}
"""

# The sand's physics, pure (no DOM, no clock: node tests run it alone). Each grain's state is an OFFSET from its
# resting place on its sieve, in world units (y up), with a velocity: gravity pulls it down, it lands on the plane
# with a soft bounce and floor friction, and a grain that has lain still on the floor for REST seconds creeps slowly
# back home (the sieve's own shaking: a furrow lingers, then heals over several seconds) so the picture recovers. A grain pushed past the sieve's edge, or landing hard on a mesh hole (a fixed share of
# grains; never one still fading in), falls through, fades out below and comes back from above, fading in. Heavy
# grains (the amber picks) are held by a stiff damped spring: they only wobble. Velocities are capped and damped; a
# grain at rest sleeps (skipped). The caller integrates with fixed steps (e.g. 1/120 s) and applies the pointer's
# forces with push()/radial().
CORE = r"""
var SandCore=(function(){
var P={g:5.5,air:1.3,fric:5.5,bounce:0.32,home:2,rest:1.3,snap:0.003,hk:60,hc:9,heavy:5,vmax:4.5,hole:1.1,holes:0.28,drop:0.9,resp:0.35,
 fadeIn:2.4,sleep:6e-4};
function make(n){function f(){return new Float32Array(n);}var S={n:n,x:f(),y:f(),z:f(),vx:f(),vy:f(),vz:f(),hu:f(),hv:f(),
 ha:f(),m:f(),vis:f(),rt:f(),aw:new Uint8Array(n),sp:new Uint8Array(n),fall:new Uint8Array(n),awake:0,moving:0};for(var i=0;i<n;i++){S.m[i]=1;S.vis[i]=1;S.ha[i]=1;}return S;}
function hash(i){var x=Math.sin(i*91.7+13.1)*43758.5453;return x-Math.floor(x);}
// a force (acceleration a for dt seconds) or, with dt=0, an impulse (a velocity change); divided by the grain's mass
// (only a real kick restarts a grain's rest: a faint push at a still cursor's rim lets it creep to a balance and stop)
function push(S,i,ax,ay,az,dt){var k=(dt>0?dt:1)/S.m[i];S.vx[i]+=ax*k;S.vy[i]+=ay*k;S.vz[i]+=az*k;S.aw[i]=1;
 if((Math.abs(ax)+Math.abs(ay)+Math.abs(az))*k>0.01)S.rt[i]=0;}
// away from a source along the world direction (dx,dy,dz), strength str at the source falling to 0 at e=0
// (e = 1 - distance/radius), with a share `lift` straight up (sand kicked up falls back)
function radial(S,i,dx,dy,dz,e,str,lift,dt){if(e<=0)return;var d=Math.sqrt(dx*dx+dy*dy+dz*dz);if(d<1e-9)return;
 var a=str*e*e;push(S,i,dx/d*a,dy/d*a+lift*a,dz/d*a,dt);}
function zero(S,i){S.x[i]=S.y[i]=S.z[i]=S.vx[i]=S.vy[i]=S.vz[i]=S.rt[i]=0;S.fall[i]=0;S.vis[i]=1;S.aw[i]=0;}
// S.awake: grains not asleep; S.moving: of those, the ones falling or moving fast enough to need 60 fps (a slow
// drift or a creep home reads the same at 30). S.sp: a grain that fell through and came back from above (sp=1)
function step(S,h){var n=S.n,aw=0,mo=0,ea=Math.exp(-P.air*h),ef=Math.exp(-P.fric*h);
 for(var i=0;i<n;i++){if(!S.aw[i])continue;
  var x=S.x[i],y=S.y[i],z=S.z[i],vx=S.vx[i],vy=S.vy[i],vz=S.vz[i],hv=S.m[i]>1.5,fl=S.fall[i];
  if(hv){vx+=(-P.hk*x-P.hc*vx)*h;vy+=(-P.hk*y-P.hc*vy)*h;vz+=(-P.hk*z-P.hc*vz)*h;}
  else{vy-=P.g*h;var sv=Math.abs(vx)+Math.abs(vz);
   if(!fl&&y<=1e-4&&(sv<0.03||(S.rt[i]>P.rest&&sv<0.3))){var rt=S.rt[i]+h,hm=rt>P.rest?P.home:0;S.rt[i]=rt;vx=(vx-hm*x*h)*ef;vz=(vz-hm*z*h)*ef;}
   else{S.rt[i]=0;if(!fl&&y<=1e-4){vx*=ef;vz*=ef;}}vx*=ea;vy*=ea;vz*=ea;}
  var sp=Math.sqrt(vx*vx+vy*vy+vz*vz);if(sp>P.vmax){var c=P.vmax/sp;vx*=c;vy*=c;vz*=c;}
  x+=vx*h;y+=vy*h;z+=vz*h;
  if(!hv){
   if(!fl){var a=S.ha[i]||1,u=S.hu[i]+x/a,v=S.hv[i]+z/a;
    if(u<-1||u>1||v<-1||v>1)fl=1;
    else if(y<0){if(vy<-P.hole&&S.vis[i]>=1&&hash(i)<P.holes)fl=1;else{y=0;vy=vy<-0.08?-vy*P.bounce:0;}}}
   if(fl){S.vis[i]=Math.max(0,1+y/P.drop);if(y<-P.drop){x=0;z=0;y=P.resp;vx=0;vy=0;vz=0;fl=0;S.vis[i]=0;S.sp[i]=1;}}
   else if(S.vis[i]<1)S.vis[i]=Math.min(1,S.vis[i]+P.fadeIn*h);}
  // asleep at home; a creeping grain within a pixel or so of home settles there (its creep is slow by then)
  var sl=(!hv&&S.rt[i]>P.rest)?P.snap:P.sleep;
  if(!fl&&S.vis[i]>=1&&Math.abs(x)<sl&&Math.abs(z)<sl&&Math.abs(y)<P.sleep&&
   Math.abs(vx)+Math.abs(vy)+Math.abs(vz)<P.sleep*4){x=y=z=vx=vy=vz=0;S.aw[i]=0;S.rt[i]=0;}else{aw++;if(fl||(S.rt[i]<=P.rest&&Math.abs(vx)+Math.abs(vy)+Math.abs(vz)>0.12))mo++;}
  S.x[i]=x;S.y[i]=y;S.z[i]=z;S.vx[i]=vx;S.vy[i]=vy;S.vz[i]=vz;S.fall[i]=fl;}
 S.awake=aw;S.moving=mo;return aw;}
return {P:P,make:make,push:push,radial:radial,zero:zero,step:step};
})();
"""

# The camera, pure (no DOM, no clock: node tests run it alone). A target view (yaw and pitch offsets from the resting
# angle, a distance, a look-at point) and the current view that follows it, damped. A drag orbits (soft limits: past
# them the drag resists, and on release a spring brings the view back; a release keeps a little inertia); a pan moves
# the look-at point in the camera plane so the point under the pointer stays under it; a zoom changes the distance
# towards the pointer (the point under it stays put); fly() and reset() only set the target (the view glides there);
# snap() makes the current view the target (reduced motion: no glide). basis() is the camera frame the page's
# projection uses: forward f, right r, up u. zoom() and pan() take (x - ox) / Fc style pointer offsets and the
# frame of the drawn view.
#
# The world (owner review, 2026-09-28): three sieves on ONE vertical axis (no sideways staircase), far enough apart
# that at the resting view no sieve hides any part of the one below it; the gap grows on a portrait screen so the
# stack fills ~3/4 of a phone's height as well (world(w, h)); the resting camera sits well above the top sieve.
# frame() fits the stack into the free part of the scene (the window minus the results panel). The camera never
# enters a sieve's slab: a move (orbit, pan, zoom, inertia, a flight, a reloaded view) that would put the camera
# within P.clr of a sieve's plane inside its footprint, or through it, is refused (side(), guard()).
CAM = r"""
var SandCam=(function(){
var P={y0:0.4,p0:0.64,d0:6.4,tx:0,ty:0,tz:0.1,limY:1.1,pMin:0.2,pMax:1.3,dMin:0.8,dMax:14,bx:1.4,by:2.6,bz:1.4,
 ky:0.004,kp:0.003,k:12,inert:3.5,spring:28,clr:0.14,gap:2,
 sa:[0.82,0.79,0.76],stl:[0.07,0.04,0.01],srl:[-0.03,0.02,-0.02],sc:[[0,2,0.1],[0,0,0.1],[0,-2,0.1]]};
function cl(v,a,b){return v<a?a:(v>b?b:v);}
// the world for a scene of w x h px: the gap between sieves (wider on a portrait screen), the resting distance
function world(w,h){var g=cl(1.95+(h/Math.max(1,w)-1)*0.45,1.95,2.5);P.gap=g;P.sc=[[0,g,0.1],[0,0,0.1],[0,-g,0.1]];
 P.d0=3.2*g;P.dMax=2.2*P.d0;P.by=g+0.6;return g;}
function make(){return {oy:0,op:0,d:P.d0,tx:P.tx,ty:P.ty,tz:P.tz,a:0,b:0,c:P.d0,x:P.tx,y:P.ty,z:P.tz,vy:0,vp:0};}
function basis(yaw,pit,o){var cp=Math.cos(pit),sp=Math.sin(pit),cy=Math.cos(yaw),sy=Math.sin(yaw);
 o.fx=-sy*cp;o.fy=-sp;o.fz=cy*cp;o.rx=cy;o.ry=0;o.rz=sy;
 o.ux=o.fy*o.rz-o.fz*o.ry;o.uy=o.fz*o.rx-o.fx*o.rz;o.uz=o.fx*o.ry-o.fy*o.rx;return o;}
// a point of sieve k at (u, v) in [-1, 1]^2 of its square, at its resting tilt (into o.x, o.y, o.z)
function spot(k,u,v,o){var c=P.sc[k],a=P.sa[k],tl=P.stl[k],rl=P.srl[k];o.x=c[0]+a*u*Math.cos(rl);
 o.y=c[1]+a*(u*Math.sin(rl)+v*Math.sin(tl));o.z=c[2]+a*v*Math.cos(tl);return o;}
// the scale and offset that fit the stack (its corners and the tops of its sand) into the free part of a w x h
// scene (iw px taken on the right, ih at the bottom) at the resting view, wherever the drift (ys yaw, ps pitch)
// takes the camera; with the stack's box on the screen (x0..x1, y0..y1)
var FB={},FP={};
function frame(w,h,iw,ih,ys,ps){var x0=1e9,x1=-1e9,y0=1e9,y1=-1e9;
 for(var yo=-1;yo<=1;yo++)for(var po=-1;po<=1;po+=2){basis(P.y0+yo*ys,P.p0+po*ps,FB);
  var cx=P.tx-FB.fx*P.d0,cy=P.ty-FB.fy*P.d0,cz=P.tz-FB.fz*P.d0;
  for(var k=0;k<3;k++)for(var c=0;c<5;c++){if(c<4)spot(k,c&1?1:-1,c&2?1:-1,FP);else{spot(k,0,0,FP);FP.y+=0.3*P.sa[k];}
   var dx=FP.x-cx,dy=FP.y-cy,dz=FP.z-cz,zc=dx*FB.fx+dy*FB.fy+dz*FB.fz;if(zc<0.15)continue;
   var X=(dx*FB.rx+dy*FB.ry+dz*FB.rz)/zc,Y=-(dx*FB.ux+dy*FB.uy+dz*FB.uz)/zc;
   x0=Math.min(x0,X);x1=Math.max(x1,X);y0=Math.min(y0,Y);y1=Math.max(y1,Y);}}
 var vw=Math.max(60,w-iw),vh=Math.max(60,h-ih),nar=vw<560;
 var a0=vw*(nar?0.05:0.1),a1=vw*(nar?0.95:0.9),b0=vh*(nar?0.12:0.1),b1=vh*(nar?0.9:0.92);
 var F=Math.min((a1-a0)/Math.max(1e-6,x1-x0),(b1-b0)/Math.max(1e-6,y1-y0));
 var ox=(a0+a1)/2-F*(x0+x1)/2,oy=(b0+b1)/2-F*(y0+y1)/2;
 return {Fc:F,ox:ox,oy:oy,x0:ox+F*x0,x1:ox+F*x1,y0:oy+F*y0,y1:oy+F*y1};}
// where the target view puts the camera, and how it stands to each sieve: s[k] = +1 above / -1 below its plane
// while inside its footprint (0 outside it); bad when it is inside a footprint within P.clr of the plane
var SB={},SD={s:[0,0,0],bad:false};
function side(C,o){o=o||{s:[0,0,0],bad:false};basis(P.y0+C.oy,P.p0+C.op,SB);var X=C.tx-SB.fx*C.d,Y=C.ty-SB.fy*C.d,Z=C.tz-SB.fz*C.d;
 o.bad=false;for(var k=0;k<3;k++){var c=P.sc[k],a=P.sa[k]+0.1,dx=X-c[0],dz=Z-c[2];o.s[k]=0;
  if(Math.abs(dx)>a||Math.abs(dz)>a)continue;var hh=Y-(c[1]+Math.tan(P.srl[k])*dx+Math.tan(P.stl[k])*dz);
  o.s[k]=hh<0?-1:1;if(Math.abs(hh)<P.clr)o.bad=true;}return o;}
function keep(C){return [C.oy,C.op,C.d,C.tx,C.ty,C.tz,C.x,C.y,C.z];}
// after a move: refused (the view put back, no glide) when it brings a clear camera into a sieve's slab or through
// its plane; a camera that was not clear (a new screen shape) may move anywhere
var S0={s:[0,0,0],bad:false},S1={s:[0,0,0],bad:false};
function guard(C,k0){if(S0.bad)return true;side(C,S1);var bad=S1.bad;
 for(var k=0;k<3;k++)if(S1.s[k]&&S0.s[k]&&S1.s[k]!==S0.s[k])bad=true;if(!bad)return true;
 C.oy=k0[0];C.op=k0[1];C.d=k0[2];C.tx=k0[3];C.ty=k0[4];C.tz=k0[5];C.x=k0[6];C.y=k0[7];C.z=k0[8];C.vy=C.vp=0;return false;}
function over(v,a,b){return v>b?v-b:(v<a?v-a:0);}
function box(C){C.tx=cl(C.tx,-P.bx,P.bx);C.ty=cl(C.ty,-P.by,P.by);C.tz=cl(C.tz,-P.bz,P.bz);
 C.x=cl(C.x,-P.bx,P.bx);C.y=cl(C.y,-P.by,P.by);C.z=cl(C.z,-P.bz,P.bz);}
// a drag of (dx, dy) px over dt s: yaw and pitch, resisting past the limits; the release velocity is tracked
function orbit(C,dx,dy,dt){var p0=P.pMin-P.p0,p1=P.pMax-P.p0,k0=keep(C);side(C,S0);
 var ky=P.ky*(Math.abs(C.oy)>P.limY?0.3:1),kp=P.kp*(over(C.op,p0,p1)?0.3:1);
 C.oy=cl(C.oy+dx*ky,-P.limY-0.3,P.limY+0.3);C.op=cl(C.op+dy*kp,p0-0.15,p1+0.15);
 if(!guard(C,k0))return false;
 if(dt>0){var g=1-Math.exp(-12*dt);C.vy+=(dx*ky/dt-C.vy)*g;C.vp+=(dy*kp/dt-C.vp)*g;}return true;}
// the look-at point follows the pointer (both the target and the current view: a pan is direct)
function pan(C,dx,dy,F,o){var k0=keep(C);side(C,S0);var s=C.c/Math.max(1e-6,F),mx=(-dx*o.rx+dy*o.ux)*s,my=(-dx*o.ry+dy*o.uy)*s,mz=(-dx*o.rz+dy*o.uz)*s;
 C.tx+=mx;C.ty+=my;C.tz+=mz;C.x+=mx;C.y+=my;C.z+=mz;box(C);return guard(C,k0);}
// the distance times f about the world point (px, py, pz): the camera moves along its ray to that point, so the point
// stays where it is on the screen (the look-at point scales towards it by the same factor)
function zoomAbout(C,f,px,py,pz){var d0=C.d,d1=cl(d0*f,P.dMin,P.dMax);if(d1===d0)return false;var g=d1/d0,k0=keep(C);side(C,S0);
 C.tx=px+g*(C.tx-px);C.ty=py+g*(C.ty-py);C.tz=pz+g*(C.tz-pz);C.d=d1;box(C);return guard(C,k0);}
// ... about the point under the pointer on the plane through the look-at point, square to the view: (ax, ay) =
// ((x - ox) / Fc, -(y - oy) / Fc)
function zoom(C,f,ax,ay,o){var d=C.d;return zoomAbout(C,f,C.tx+d*(ax*o.rx+ay*o.ux),C.ty+d*(ax*o.ry+ay*o.uy),C.tz+d*(ax*o.rz+ay*o.uz));}
// a flight to a point at distance d; where that would put the camera in a sieve's slab, it looks down more steeply,
// then from further away
function fly(C,x,y,z,d){C.tx=x;C.ty=y;C.tz=z;C.d=cl(d,P.dMin,P.dMax);C.vy=C.vp=0;box(C);
 for(var i=0;i<12&&side(C,SD).bad;i++)C.op=Math.min(P.pMax-P.p0,C.op+0.06);
 for(var j=0;j<20&&side(C,SD).bad;j++)C.d=Math.min(P.dMax,C.d*1.12);}
function reset(C){C.oy=C.op=0;C.d=P.d0;C.tx=P.tx;C.ty=P.ty;C.tz=P.tz;C.vy=C.vp=0;}
function snap(C){C.a=C.oy;C.b=C.op;C.c=C.d;C.x=C.tx;C.y=C.ty;C.z=C.tz;}
// one step of dt s: inertia and the springs back inside the limits (not while held; refused like a drag where they
// would carry the camera into a sieve), then the current view follows
function step(C,dt,held){var p0=P.pMin-P.p0,p1=P.pMax-P.p0;
 if(!held&&(C.vy||C.vp||over(C.oy,-P.limY,P.limY)||over(C.op,p0,p1))){var k0=keep(C);side(C,S0);
  C.oy+=C.vy*dt;C.op+=C.vp*dt;var e=Math.exp(-P.inert*dt);C.vy*=e;C.vp*=e;
  var ey=over(C.oy,-P.limY,P.limY),ep=over(C.op,p0,p1);
  if(ey)C.vy=C.vy*Math.exp(-6*dt)-ey*P.spring*dt;if(ep)C.vp=C.vp*Math.exp(-6*dt)-ep*P.spring*dt;
  if(Math.abs(C.vy)<1e-5)C.vy=0;if(Math.abs(C.vp)<1e-5)C.vp=0;guard(C,k0);}
 var g=1-Math.exp(-P.k*dt);C.a+=(C.oy-C.a)*g;C.b+=(C.op-C.b)*g;C.c+=(C.d-C.c)*g;C.x+=(C.tx-C.x)*g;C.y+=(C.ty-C.y)*g;C.z+=(C.tz-C.z)*g;}
function settled(C){return Math.abs(C.oy-C.a)+Math.abs(C.op-C.b)+Math.abs(C.d-C.c)+Math.abs(C.tx-C.x)+Math.abs(C.ty-C.y)+
 Math.abs(C.tz-C.z)+Math.abs(C.vy)+Math.abs(C.vp)<2e-4;}
function moved(C){return Math.abs(C.oy)+Math.abs(C.op)+Math.abs(C.d-P.d0)+Math.abs(C.tx-P.tx)+Math.abs(C.ty-P.ty)+Math.abs(C.tz-P.tz)>1e-4;}
// what a reload keeps (inside the limits; nothing when the view is the resting one) and how it comes back (a view
// that would sit in a sieve's slab comes back as the resting one)
function r4(v){return Math.round(v*1e4)/1e4;}
function state(C){if(!moved(C))return null;return {oy:r4(cl(C.oy,-P.limY,P.limY)),op:r4(cl(C.op,P.pMin-P.p0,P.pMax-P.p0)),
 d:r4(C.d),tx:r4(C.tx),ty:r4(C.ty),tz:r4(C.tz)};}
function load(C,m){if(!m)return;function n(k,a,b,d){var v=m[k];return (typeof v==='number'&&isFinite(v))?cl(v,a,b):d;}
 C.oy=n('oy',-P.limY,P.limY,0);C.op=n('op',P.pMin-P.p0,P.pMax-P.p0,0);C.d=n('d',P.dMin,P.dMax,P.d0);
 C.tx=n('tx',-P.bx,P.bx,P.tx);C.ty=n('ty',-P.by,P.by,P.ty);C.tz=n('tz',-P.bz,P.bz,P.tz);if(side(C,SD).bad)reset(C);snap(C);}
return {P:P,world:world,frame:frame,spot:spot,side:side,make:make,basis:basis,orbit:orbit,pan:pan,zoom:zoom,zoomAbout:zoomAbout,
 fly:fly,reset:reset,snap:snap,step:step,settled:settled,moved:moved,state:state,load:load};
})();
"""

# The breathing cycle (owner, 2026-09-29, after tianzhi.live), pure (no DOM, no clock: node tests run it alone).
# While nobody uses the page (no pointer moved with intent, no drag, wheel, click or key for P.idle s, no tooltip, no
# stage rush), a ~22 s cycle runs: the camera eases to a view from above while the three sieves come down onto one
# plane (the middle sieve's), one size, untilted, so they read as one orderly grid; it holds briefly (~2-3 s), then the
# stack expands back along the vertical axis into the 3D stack and its slow orbit (more than half of the cycle). Only damped values (two damped lerps in a row:
# no jump in speed at a start or a turn, never a tween), the deeper a sieve the slower it follows. Any use of the
# page cancels the cycle (the values glide back at a faster rate; a press, the wheel or a key hands the camera over at
# once, hand()) and the idle count starts again; a rush cancels it
# too (it takes priority); reduced motion never runs it. f[0..2]: each sieve's flatness, f[3]: the camera's lift to
# the view from above (0 the 3D stack, 1 the flat grid); state()/load()/advance() carry it across the page's 3 s
# reloads (sessionStorage).
BREATH = r"""
var SandBreath=(function(){
var P={idle:6,cyc:22,flat:6.5,k:1.2,dk:0.12,kc:2.6,pit:1.42,near:0.3,fill:0.72};
function make(){return {p:-1,idle:0,g:[0,0,0,0],f:[0,0,0,0]};}
function poke(B){B.idle=0;B.p=-1;}
// a real use (a press, the wheel, a key): the camera is the user's at once (the page folds the lifted view into its
// own camera first, so nothing jumps); only the sieves glide back
function hand(B){poke(B);B.f[3]=0;B.g[3]=0;}
function target(B){return B.p>=0&&(B.p%P.cyc)<P.flat?1:0;}
function step(B,dt,o){o=o||{};dt=dt>0?Math.min(dt,0.25):0;var k;
 if(o.reduced){B.p=-1;B.idle=0;for(k=0;k<4;k++){B.g[k]=0;B.f[k]=0;}return B;}
 if(o.rush||o.busy){B.idle=0;B.p=-1;}
 else{B.idle+=dt;if(B.p>=0)B.p+=dt;else if(B.idle>=P.idle)B.p=0;}
 var tg=target(B),r=B.p<0?P.kc:P.k;
 for(k=0;k<4;k++){var a=1-Math.exp(-r*(k<3?1-P.dk*k:1)*dt);B.g[k]+=(tg-B.g[k])*a;B.f[k]+=(B.g[k]-B.f[k])*a;
  if(!tg&&B.f[k]<1e-4&&B.g[k]<1e-4){B.f[k]=0;B.g[k]=0;}}
 return B;}
function active(B){return B.p>=0||B.f[0]+B.f[1]+B.f[2]+B.f[3]>0;}
function r4(v){return Math.round(v*1e4)/1e4;}
function state(B){return {p:r4(B.p),i:r4(B.idle),g:B.g.map(r4),f:B.f.map(r4)};}
function load(B,m){if(!m||typeof m!=='object')return B;function ok(v,a,b){return typeof v==='number'&&isFinite(v)&&v>=a&&v<=b;}
 if(ok(m.p,-1,1e6))B.p=m.p<0?-1:m.p;if(ok(m.i,0,1e6))B.idle=m.i;
 for(var k=0;k<4;k++){if(m.g&&ok(m.g[k],0,1))B.g[k]=m.g[k];if(m.f&&ok(m.f[k],0,1))B.f[k]=m.f[k];}return B;}
// the time a reload took (at most 20 s), stepped as if the page had run on
function advance(B,sec,o){var n=Math.min(600,Math.ceil(Math.max(0,Math.min(20,sec))*30));for(var i=0;i<n;i++)step(B,Math.min(20,sec)/n,o);return B;}
return {P:P,make:make,poke:poke,hand:hand,step:step,target:target,active:active,state:state,load:load,advance:advance};
})();
"""

# The reading log's typing, pure (no DOM, no clock: node tests run it alone). feed() queues the lines whose sequence
# number it has not seen (a burst keeps only the newest P.back to type; `type` shows all but the newest few at once);
# the numbers start again when their source changes (og: another job, or the rows) or run backwards. step() types the
# current line at a constant P.cps, pauses briefly between lines and scrolls a finished one into the column (at most
# P.max kept). With nothing new for P.idle s it goes over the run's lines again, quietly (slower, never the bright
# typing edge: L.rep), one every P.every s, oldest first: a line no longer in the column is typed in at its foot, and
# when every line is in the column one is typed again in its own row (L.ri); a line never shows twice. state()/load()
# carry it across reloads.
READLOG = r"""
var SandLog=(function(){
var P={cps:30,rcps:16,max:14,gap:0.45,idle:9,every:6,back:10,hist:60,len:80};
function make(){return {lines:[],cur:'',c:0,q:[],n:0,og:null,hist:[],quiet:0,wait:0,rp:-1,rep:false,ri:-1};}
function push(L,s){L.lines.push(s);if(L.lines.length>P.max)L.lines.shift();}
function ok(e){return e&&typeof e[0]==='number'&&isFinite(e[0])&&typeof e[1]==='string';}
function quiet(L){if(L.rep){L.cur='';L.c=0;L.rep=false;L.ri=-1;L.wait=0;}}
function feed(L,evs,type,og){var add=[],i,mx=-1;evs=evs||[];
 if(typeof og==='string'&&og!==L.og){if(L.og!==null)L.n=0;L.og=og;}
 for(i=0;i<evs.length;i++)if(ok(evs[i])&&evs[i][0]>mx)mx=evs[i][0];
 if(mx>=0&&mx<L.n)L.n=0;                       // the numbers ran backwards: another run's, counted again
 for(i=0;i<evs.length;i++){var e=evs[i];if(ok(e)&&e[0]>L.n){add.push(e[1].slice(0,P.len));L.n=e[0];}}
 if(add.length)quiet(L);                       // a real answer never waits behind a quiet line
 if(type!==undefined&&add.length>type){var now=add.slice(0,add.length-type);for(i=0;i<now.length;i++)push(L,now[i]);add=add.slice(add.length-type);}
 for(i=0;i<add.length;i++)L.q.push(add[i]);if(L.q.length>P.back)L.q=L.q.slice(L.q.length-P.back);
 L.hist=[];for(i=Math.max(0,evs.length-P.hist);i<evs.length;i++)if(ok(evs[i]))L.hist.push(evs[i][1].slice(0,P.len));
 if(add.length)L.quiet=0;return add.length;}
function shown(L){return L.cur?L.cur.slice(0,Math.floor(L.c)):'';}
// the next quiet line: the oldest after the last one that is not in the column (typed at its foot), else the next one
// again in its own row
function again(L){var n=L.hist.length,j,x;
 for(j=1;j<=n;j++){x=(L.rp+j)%n;if(L.lines.indexOf(L.hist[x])<0&&L.hist[x]!==L.cur){L.rp=x;L.ri=-1;return L.hist[x];}}
 x=(L.rp+1)%n;L.rp=x;L.ri=L.lines.indexOf(L.hist[x]);return L.ri>=0?L.hist[x]:'';}
// 0: nothing changed, 1: the line being typed grew, 2: a line was finished (or a new one begun)
function step(L,dt){if(!(dt>0))return 0;dt=Math.min(dt,0.25);
 if(!L.cur){if(L.wait>0){L.wait-=dt;return 0;}
  if(L.q.length){L.cur=L.q.shift();L.rep=false;L.ri=-1;L.quiet=0;}
  else{L.quiet+=dt;if(L.quiet<P.idle||!L.hist.length)return 0;L.quiet=P.idle-P.every;var s=again(L);if(!s)return 0;L.cur=s;L.rep=true;}
  L.c=0;return 2;}
 var c0=Math.floor(L.c);L.c+=(L.rep?P.rcps:P.cps)*dt;
 if(L.c>=L.cur.length){if(L.ri<0)push(L,L.cur);L.cur='';L.c=0;L.ri=-1;L.wait=P.gap;return 2;}
 return Math.floor(L.c)!==c0?1:0;}
function state(L){return {n:L.n,og:L.og,lines:L.lines.slice(),cur:L.cur,c:Math.floor(L.c),q:L.q.slice(),rp:L.rp,quiet:Math.round(L.quiet*100)/100,rep:L.rep,ri:L.ri};}
function strs(a,m){var o=[];if(!a||typeof a.length!=='number')return o;for(var i=Math.max(0,a.length-m);i<a.length;i++)if(typeof a[i]==='string')o.push(a[i].slice(0,P.len));return o;}
function load(L,m){if(!m||typeof m!=='object')return L;if(typeof m.n==='number'&&isFinite(m.n))L.n=m.n;if(typeof m.og==='string')L.og=m.og.slice(0,16);
 L.lines=strs(m.lines,P.max);L.q=strs(m.q,P.back);
 L.cur=typeof m.cur==='string'?m.cur.slice(0,P.len):'';L.c=typeof m.c==='number'&&m.c>=0?Math.min(m.c,L.cur.length):0;
 if(typeof m.rp==='number'&&isFinite(m.rp))L.rp=m.rp;if(typeof m.quiet==='number'&&isFinite(m.quiet))L.quiet=m.quiet;L.rep=!!m.rep&&!!L.cur;
 L.ri=(L.rep&&typeof m.ri==='number'&&m.ri>=0&&m.ri<L.lines.length&&L.lines[m.ri]===L.cur)?m.ri:-1;
 if(L.rep&&L.ri<0&&L.lines.indexOf(L.cur)>=0){L.cur='';L.c=0;L.rep=false;}   // never a second copy of a line
 return L;}
return {P:P,make:make,feed:feed,step:step,shown:shown,state:state,load:load};
})();
"""

# The drawing. Pure canvas 2D, no allocation per frame: <= 1,600 grains in typed arrays, bucketed by layer (for
# occlusion) and brightness into a few paths; three meshes of 12 x 12 cells in three depth-bucketed strokes; 150 far
# stars. World: y up, the sieves stacked downwards and deeper; a close camera ~40 degrees above, orbiting slowly;
# every point goes through one perspective projection (prj). The stack is framed to ~80 % of the scene's free part
# (the window minus the results panel, page_hud's inset()). Drawn once at once on load, then at ~30 fps, 60 during
# the opening, a rush, a camera move or while someone plays.
#
# Interaction (owner, 2026-09-28: the page feels alive, the cursor physically moves the sand; restrained, damped,
# nothing snaps): the mouse tilts the scene a little (parallax); the camera is a model viewer (CAM above); resting on
# a sieve (or focusing it with the keys) shows that stage's real numbers in a tooltip beside its rim and lights that
# layer (the others dim); an amber grain shows the company and a click opens its row in the results panel; a row
# pointed at in the panel lights its grain and names it. The pointer is a force field in the scene (CORE): grains
# near its ray are pushed along the ground, a moving cursor leaves a short wake, a click sends a radial shockwave.
# Grains are binned by screen cell each frame, so the pointer only looks at the few cells around it. Reduced motion:
# no physics, no parallax, no drift; tooltips, rows and the controls work (the view jumps).
# window.__jevSand is the HUD's handle: light(rank), relayout(), pause(bool), reset().
JS = r"""
(function(){try{
""" + CORE + CAM + BREATH + READLOG + r"""
var box=document.getElementById('sand');if(!box||!box.getAttribute||!box.querySelector)return;
// the scene runs only under the HUD (page_hud turns it on); without it the plain page stays
var DE=document.documentElement;if(DE&&typeof DE.className==='string'&&DE.className.indexOf('hud-on')<0)return;
var cv=box.querySelector('canvas');var W=window;if(!cv||!cv.getContext||!W.requestAnimationFrame)return;
var ctx=cv.getContext('2d');if(!ctx)return;
var HUD=W.__jevHud||null;
function at(k){var v=box.getAttribute('data-'+k);return v===null?'':v;}
function nb(k){var v=at(k);return v===''?null:Number(v);}
var LANG=at('lang'),STATE=at('state'),STAGE=nb('stage')||0,ACT=nb('active'),FRAC=nb('frac'),DOTS=nb('dots')||0;
var TIP=[at('tip0'),at('tip1'),at('tip2')],TIPCLICK=at('tip-click'),TIPAGAIN=at('tip-again');
var NUM=[];for(var ni=0;ni<3;ni++){var np=at('n'+ni).split(',');NUM.push([np[0]?Number(np[0]):null,np[1]?Number(np[1]):null]);}
var CNT=at('cnt').split(',');for(var ci=0;ci<4;ci++)CNT[ci]=(CNT[ci]===undefined||CNT[ci]==='')?null:Number(CNT[ci]);
var PICKS=[];try{PICKS=JSON.parse(at('picks')||'[]')||[];}catch(e){PICKS=[];}
try{var hb=Date.parse(at('stale-since'));if(!isNaN(hb)&&Date.now()-hb>(nb('stale-after')||0)*1000){STATE='fail';ACT=null;FRAC=null;}}catch(e){}
if(STATE!=='run'){ACT=null;}
var KEY='jevscreen-sand:'+at('key'),REDUCED=false;
try{REDUCED=!!(W.matchMedia&&W.matchMedia('(prefers-reduced-motion: reduce)').matches);}catch(e){}
var MEM={};try{MEM=JSON.parse(W.sessionStorage.getItem(KEY)||'{}')||{};}catch(e){MEM={};}
var now0=Date.now(),perf0=(W.performance&&performance.now)?performance.now():now0;
function clock(){return (W.performance&&performance.now)?performance.now():Date.now();}
// the phase goes on across reloads: the time of the last page plus the time since it was left
var T0=(typeof MEM.t==='number'&&typeof MEM.at==='number'&&now0>=MEM.at&&now0-MEM.at<36e5)?MEM.t+(now0-MEM.at)/1000:0;
// a live transition: the last page of this key was left seconds ago in this tab (the page reloads every 3 s).
// Only then does a new stage shake its sieve; an old memory (a page opened later) plays nothing.
var LIVE=typeof MEM.at==='number'&&now0>=MEM.at&&now0-MEM.at<15000;
var HOP=0.62,RD=0.45,FLY=1.15,FLD=2.6;
// a new stage since the last page: the shake and the rush (resumed when a reload cut it short), played once
var TR=null;
if(LIVE&&MEM.tr&&typeof MEM.tr.from==='number'&&now0-MEM.tr.at<(MEM.tr.to-MEM.tr.from)*HOP*1000+200){TR={from:MEM.tr.from,to:Math.max(MEM.tr.to,STAGE),t:T0-(now0-MEM.tr.at)/1000};}
if(LIVE&&typeof MEM.stage==='number'&&STAGE>MEM.stage){var fr=TR?TR.from:MEM.stage;TR={from:fr,to:STAGE,t:TR?TR.t:T0};}
// a progress share rolls (fast, ~0.45 s) from what the last page showed; reduced motion shows the current one at once
// a gauge never runs backwards within a stage: a live reload that reads a smaller share keeps the one shown
if(LIVE&&FRAC!==null&&typeof MEM.f==='number'&&MEM.a===ACT&&MEM.stage===STAGE&&MEM.f>FRAC)FRAC=MEM.f;
var ROLL0=T0,PFRAC=(typeof MEM.f==='number'&&MEM.a===ACT)?MEM.f:FRAC;
if(REDUCED){TR=null;PFRAC=FRAC;}
// a live finish: the panel waits until the gold has linked up (the HUD opens it then)
if(TR&&TR.to===4&&HUD&&HUD.hold){try{HUD.hold();}catch(e){}}
// the camera (kept across reloads, also a view set under reduced motion) and the mouse's tilt (never under it)
box.className='sand on';
var CP=SandCam.P;try{var R0=box.getBoundingClientRect();SandCam.world(R0.width||W.innerWidth||1280,R0.height||W.innerHeight||800);}catch(e){}
var PARY=0.07,PARP=0.04,CAM=SandCam.make();SandCam.load(CAM,MEM);
var PART=[(!REDUCED&&typeof MEM.px==='number')?cl(MEM.px,-PARY,PARY):0,(!REDUCED&&typeof MEM.py==='number')?cl(MEM.py,-PARP,PARP):0],PAR=[PART[0],PART[1]];
var PAUSED=!!(DE&&typeof DE.className==='string'&&DE.className.indexOf('hud-text')>=0);
// the breathing cycle (SandBreath): it goes on across a live reload (the seconds the reload took stepped through)
var BR=SandBreath.make(),BP=SandBreath.P;if(!REDUCED&&LIVE&&MEM.br){SandBreath.load(BR,MEM.br);SandBreath.advance(BR,(now0-MEM.at)/1000,{});}
// the opening (the camera flies into the star sea) plays on the first view of this idea in this tab
var OP=(!REDUCED&&typeof MEM.at!=='number')?T0:null;
// ---- world: three sieves, top to bottom (market-cap floor, first read, annual-report check), lower ones deeper
var NM=1600,GR=12,RES=320,NB=4;
var SA=CP.sa,STL=CP.stl,SRL=CP.srl;          // the sieves' places are CP.sc (SandCam.world: one vertical axis)
var SVOF=[-1,0,-1,1,2];                       // the sieve that shakes when stage s is reached (none for the profiles)
var SDONE=[1,3,4];                            // the stage at which each sieve's work is done
function rnd(s){var x=Math.sin(s*127.1+311.7)*43758.5453;return x-Math.floor(x);}
function gauss(s){var a=Math.max(1e-6,rnd(s)),b=rnd(s+0.37);return Math.sqrt(-2*Math.log(a))*Math.cos(6.2832*b);}
function cl(v,a,b){return v<a?a:(v>b?b:v);}
var GU=new Float32Array(3*NM),GV=new Float32Array(3*NM),GH=new Float32Array(3*NM),GD=new Float32Array(NM),GZ=new Float32Array(NM);
for(var gi=0;gi<NM;gi++){for(var gk=0;gk<3;gk++){var gu=cl(gauss(gi*7.13+gk*101.7)*0.37,-0.93,0.93),gv=cl(gauss(gi*3.71+gk*57.3+0.5)*0.37,-0.93,0.93);
 GU[gk*NM+gi]=gu;GV[gk*NM+gi]=gv;GH[gk*NM+gi]=0.012+0.075*Math.exp(-(gu*gu+gv*gv)/0.28)*rnd(gi*3.3+gk);}
 GD[gi]=rnd(gi*5.7)*0.34;GZ[gi]=0.6+0.8*rnd(gi*9.1);}
// how many grains: a compressed share of the real counts (the counts themselves are in the tooltips)
function share(a,b,d){return (a!==null&&b!==null&&b>0&&a>=0)?Math.min(1,a/b):d;}
var N=[NM,0,0];N[1]=Math.round(cl(NM*Math.sqrt(share(CNT[1],CNT[0],0.15)),60,NM));N[2]=Math.round(cl(N[1]*Math.sqrt(share(CNT[2],CNT[1],0.04)),16,N[1]));
var NG=Math.min(DOTS,N[2],10);
function cntAt(s){return s>=4?NG:(s>=3?N[2]:(s>=1?N[1]:N[0]));}
function levAt(s){return s>=3?2:(s>=1?1:0);}
// the amber grains' places on the last sieve: a loose chain from its far side to its near side
var GLU=[],GLV=[];for(var gj=0;gj<10;gj++){GLU.push(NG>1?0.12*Math.sin(gj*2.3+0.6):0.1);GLV.push(NG>1?0.55-1.1*gj/(NG-1):0);}
// ---- the sand's physics state: one offset per grain (NM) and per amber grain (10 heavy ones after them)
var NS=NM+10,SS=SandCore.make(NS);for(var gq=NM;gq<NS;gq++)SS.m[gq]=SandCore.P.heavy;
var SK=new Int8Array(NM),SID=new Uint16Array(NS);for(var sk0=0;sk0<NM;sk0++)SK[sk0]=-1;
// ---- camera: looking down at ~40 degrees from the front right, well above the top sieve, orbiting slowly;
// perspective projection
var YAW0=CP.y0,PIT0=CP.p0,D0=CP.d0,FLYD=2.2*D0,ORB=0.08,BS={};
var Cx,Cy,Cz,fx,fy,fz,rx,ry,rz,ux,uy,uz,DIST=D0,Fc=1,ox=0,oy=0,PX=0,PY=0,PZ=0,PS=0,XC=0,YC=0,WX=0,WY=0,WZ=0,ZC=2.4;
function setCam(yaw,pit,dist,tx,ty,tz){SandCam.basis(yaw,pit,BS);fx=BS.fx;fy=BS.fy;fz=BS.fz;rx=BS.rx;ry=BS.ry;rz=BS.rz;ux=BS.ux;uy=BS.uy;uz=BS.uz;
 Cx=tx-fx*dist;Cy=ty-fy*dist;Cz=tz-fz*dist;DIST=dist;}
function prj(x,y,z){var dx=x-Cx,dy=y-Cy,dz=z-Cz;var zc=dx*fx+dy*fy+dz*fz;if(zc<0.15)return false;var s=Fc/zc;
 XC=dx*rx+dy*ry+dz*rz;YC=dx*ux+dy*uy+dz*uz;PX=ox+XC*s;PY=oy-YC*s;PZ=zc;PS=s;return true;}
function flyD(t){if(OP===null)return 0;var e=(t-OP)/FLY;return e>=1?0:(e<=0?FLYD:FLYD*Math.pow(1-e,3));}
function flyAt(t){if(OP===null)return -1;var e=(t-OP)/FLY;return e>=1||e<0?-1:e;}
var FOC=0,FOCD=0;
// the drawn view: the resting camera plus the user's view (CAM), the slow drift, the tilt and a slight lean towards the
// working sieve (FOCD: a zoom or a flight aims at a point in the drawn view, so they take it out again)
// the breathing lifts the drawn camera towards the view from above (BR.f[3]), a little nearer, looking at the plane
// the sieves come down onto, and turns it square to the grid (YA: the square-aligned yaw nearest the view as the lift
// begins; the orbit fades out with the lift): the grid's lines run across and up the screen. The user's own view (CAM)
// is untouched, until a press, the wheel or a key folds the lifted view into it (handover)
var YA=0;
function camAt(t,rest){if(rest){setCam(YAW0,PIT0,D0,CP.tx,CP.ty,CP.tz);return;}
 var still=REDUCED,orb=still?0:ORB*Math.sin(t*6.2832/41);FOCD=still?0:FOC;var tp=BR.f[3];
 var pit=PIT0+CAM.b+(still?0:0.02*Math.sin(t*6.2832/23))+PAR[1];pit+=(BP.pit-pit)*tp;
 var yaw=YAW0+CAM.a+orb+PAR[0];if(tp<1e-4)YA=Math.round(yaw/1.5708)*1.5708;yaw+=(YA-yaw)*tp;
 // from above, the grid (its diagonal) fills at most BP.fill of the free part's shorter side
 var d=CAM.c+flyD(t);if(tp>0){d+=(liftD(d)-d)*tp;}
 setCam(yaw,pit,d,CAM.x,(CAM.y+FOCD)*(1-tp),CAM.z);}
function liftD(d){return Math.max(d*(1-BP.near),2.83*SA[1]*Fc/(BP.fill*Math.max(60,Math.min(w-INS[0],h-INS[1]))));}
// a press, the wheel or a key: the view drawn this moment becomes the user's own (CAM: yaw, pitch and the look-at
// height shifted by what the lift adds; the distance drawn now, its target the user's own zoom, which the view then
// follows like any zoom), the lift is dropped at once (SandBreath.hand): the user drives the camera from the first
// frame and nothing jumps. A view that would sit in a sieve's slab keeps the glide instead
function handover(){if(REDUCED||!(BR.f[3]>1e-4)){SandBreath.poke(BR);return;}
 var tp=BR.f[3],t=T(),orb=ORB*Math.sin(t*6.2832/41);
 var yaw=YAW0+CAM.a+orb+PAR[0],dy=(YA-yaw)*tp,pit=PIT0+CAM.b+0.02*Math.sin(t*6.2832/23)+PAR[1],dp=(BP.pit-pit)*tp;
 var d=CAM.c+flyD(t),dd=(liftD(d)-d)*tp,dh=-(CAM.y+FOCD)*tp;
 var k0=[CAM.a,CAM.oy,CAM.b,CAM.op,CAM.c,CAM.d,CAM.y,CAM.ty];
 CAM.a+=dy;CAM.oy+=dy;CAM.b+=dp;CAM.op+=dp;CAM.c=cl(CAM.c+dd,CP.dMin,CP.dMax);CAM.y+=dh;CAM.ty+=dh;
 if(SandCam.side(CAM).bad){CAM.a=k0[0];CAM.oy=k0[1];CAM.b=k0[2];CAM.op=k0[3];CAM.c=k0[4];CAM.d=k0[5];CAM.y=k0[6];CAM.ty=k0[7];SandBreath.poke(BR);return;}
 SandBreath.hand(BR);}
// ---- the sieves' planes this frame: a little breathing (less for the deeper ones) and the shake
var G=[];for(var sk=0;sk<3;sk++)G.push({x:0,y:0,z:0,ax:1,ay:0,az:0,bx:0,by:0,bz:1,a:SA[sk],sh:0});
// the breathing (BR.f[k]: 1 flat) brings each sieve down onto the middle one's plane, at its size, untilted: one grid
// (the rims nest a whole cell inside each other, RS, on the grid's own lines, so each stage's progress still shows)
var RS=[1,1,1];
function sieves(t,rest){var hp=rest?null:hopAt(t);for(var k=0;k<3;k++){var g=G[k],dep=1-0.3*k,br=(rest||REDUCED)?0:1,fk=rest?0:BR.f[k],ek=1-fk;
 var tl=(STL[k]+br*0.014*dep*Math.sin(t*0.83+k*1.3))*ek,rl=(SRL[k]+br*0.01*dep*Math.sin(t*0.61+k*2.1))*ek,sh=0;
 if(hp&&SVOF[hp.to]===k&&hp.u<0.34){var q=hp.u/0.34;sh=0.075*Math.sin(q*9.4248)*(1-q);}
 g.ax=Math.cos(rl);g.ay=Math.sin(rl);g.az=0;g.bx=0;g.by=Math.sin(tl);g.bz=Math.cos(tl);
 var c=CP.sc[k],c1=CP.sc[1];g.x=c[0]+g.ax*sh;g.y=(c[1]+br*0.022*dep*Math.sin(t*0.71+k*1.9))*ek+c1[1]*fk+g.ay*sh;g.z=c[2]*ek+c1[2]*fk;g.sh=sh;
 g.a=SA[k]+(SA[1]-SA[k])*fk;RS[k]=1-(2/GR)*k*fk;}}
function spt(k,u,v,hg){var g=G[k],a=g.a;WX=g.x+a*(u*g.ax+v*g.bx);WY=g.y+a*(u*g.ay+v*g.by)+hg;WZ=g.z+a*(u*g.az+v*g.bz);}
// ---- the reading log: a faint column at the lower left (the CSS hides it below 760 px) that types the run's recent
// per-company answers (data-log: security id, stage and label words only; SandLog); as wide as leaves the stack clear
// (it ends 24 px left of the stack's left edge; hidden when that leaves under 150 px). textContent only (never markup)
var LOGEL=null;try{LOGEL=box.querySelector('.sandlog');}catch(e){}if(LOGEL===cv||!LOGEL||!LOGEL.appendChild)LOGEL=null;
var LG=SandLog.make(),EVS=[],LGD=[],LGT=0,LGW=-1,LGS=at('log-from');try{EVS=JSON.parse(at('log')||'[]')||[];}catch(e){EVS=[];}
if(!EVS.length||!document.createElement)LOGEL=null;
if(LOGEL){if(!REDUCED&&LIVE&&MEM.log){SandLog.load(LG,MEM.log);SandLog.feed(LG,EVS,undefined,LGS);}else SandLog.feed(LG,EVS,REDUCED?0:3,LGS);}
// (a line typed again in its own row, LG.ri, and every quiet line keep the finished lines' look: only a real new
// answer has the bright typing edge)
function logRows(){if(!LOGEL)return;var ri=LG.cur?LG.ri:-1,want=LG.lines.length+(LG.cur&&ri<0?1:0);
 while(LGD.length>want){var d0=LGD.shift();try{LOGEL.removeChild(d0);}catch(e){}}
 while(LGD.length<want){var d1=document.createElement('div');LOGEL.appendChild(d1);LGD.push(d1);}
 for(var i=0;i<want;i++){var s=i===ri||i>=LG.lines.length?SandLog.shown(LG):LG.lines[i],c=i>=LG.lines.length&&!LG.rep?'cur':'';
  if(LGD[i].textContent!==s)LGD[i].textContent=s;if(LGD[i].className!==c)LGD[i].className=c;}}
function logTick(now){if(!LOGEL||REDUCED)return;var dt=LGT?(now-LGT)/1000:0;LGT=now;var r=SandLog.step(LG,dt);
 if(r===2)logRows();else if(r===1&&LGD.length){var row=LGD[LG.ri>=0&&LG.ri<LGD.length?LG.ri:LGD.length-1];row.textContent=SandLog.shown(LG);}}
function logFit(f){if(!LOGEL)return;var wd=Math.floor(Math.min(300,f.x0-16-24));if(wd===LGW)return;LGW=wd;
 try{LOGEL.style.width=Math.max(0,wd)+'px';LOGEL.style.visibility=wd<150?'hidden':'';}catch(e){}}
// fit the stack to ~80 % of the scene's free part (the window minus the results panel) at the resting view, wherever
// the drift and the tilt take the camera (SandCam.frame); the user's zoom and pan then move away from that
var w=0,h=0,dpr=1,INS=[0,0],INT=[0,0];
function hudInset(){try{var o=HUD&&HUD.inset?HUD.inset():null;return [Math.max(0,Number(o&&o.r)||0),Math.max(0,Number(o&&o.b)||0)];}catch(e){return [0,0];}}
function fit(){var f=SandCam.frame(w,h,INS[0],INS[1],ORB+PARY*0.5+0.02,PARP*0.5+0.015);Fc=f.Fc;ox=f.ox;oy=f.oy;logFit(f);}
// the window's shape sets the world (the gap between the sieves): a view nobody moved follows it
function shape(){var g0=CP.gap,still=!SandCam.moved(CAM);SandCam.world(w,h);D0=CP.d0;if(CP.gap!==g0&&still){SandCam.reset(CAM);SandCam.snap(CAM);}}
// a canvas is cleared whenever its size is set: only when it really changes (and every caller redraws)
function size(){var r=box.getBoundingClientRect();w=Math.max(1,r.width);h=Math.max(1,r.height);dpr=Math.min(2,W.devicePixelRatio||1);
 var cw=Math.round(w*dpr),ch=Math.round(h*dpr);if(cv.width!==cw)cv.width=cw;if(cv.height!==ch)cv.height=ch;shape();INT=hudInset();INS=INT.slice();fit();}
size();
// the panel slides: the framing follows it, damped (at once under reduced motion)
function insets(dt){var mv=Math.abs(INS[0]-INT[0])+Math.abs(INS[1]-INT[1]);if(mv<0.3){if(mv>0){INS=INT.slice();fit();}return false;}
 if(REDUCED){INS=INT.slice();}else{var g=1-Math.exp(-10*dt);INS[0]+=(INT[0]-INS[0])*g;INS[1]+=(INT[1]-INS[1])*g;}fit();return true;}
// ---- the star sea: points in the camera's first view (the opening flies through them), and far faint stars
var SX=new Float32Array(NM),SY=new Float32Array(NM),SZ=new Float32Array(NM),BG=150,BX=new Float32Array(BG),BY=new Float32Array(BG),BZ=new Float32Array(BG);
(function(){FLYD=2.2*D0;setCam(YAW0,PIT0,D0+FLYD,CP.tx,CP.ty,CP.tz);for(var i=0;i<NM;i++){var zc=0.6+rnd(i*1.9+4)*(FLYD+7),px=(rnd(i*2.3+1)*2-1)*zc*0.42,py=(rnd(i*4.1+2)*2-1)*zc*0.24;
 SX[i]=Cx+fx*zc+rx*px+ux*py;SY[i]=Cy+fy*zc+ry*px+uy*py;SZ[i]=Cz+fz*zc+rz*px+uz*py;}
 setCam(YAW0,PIT0,D0,CP.tx,CP.ty,CP.tz);for(var j=0;j<BG;j++){var d=D0+9+rnd(j*6.1)*16,bx=(rnd(j*8.3)*2-1)*d*0.5,by=(rnd(j*9.7)*2-1)*d*0.3;
 BX[j]=Cx+fx*d+rx*bx+ux*by;BY[j]=Cy+fy*d+ry*bx+uy*by;BZ[j]=Cz+fz*d+rz*bx+uz*by;}})();
// ---- the shake and rush of a stage change
function hopAt(t){if(!TR)return null;var e=t-TR.t;var n=TR.to-TR.from;if(e<0||n<=0)return null;var k=Math.floor(e/HOP);if(k>=n)return null;
 return {from:TR.from+k,to:TR.from+k+1,u:(e-k*HOP)/HOP};}
function reached(t){if(!TR)return STAGE;var e=t-TR.t,n=TR.to-TR.from;if(e>=n*HOP)return TR.to;return TR.from+Math.max(0,Math.floor(e/HOP));}
function rushing(t){return !!(TR&&t-TR.t<(TR.to-TR.from)*HOP);}
function ease(u){return u<0?0:(u>1?1:(u<.5?4*u*u*u:1-Math.pow(-2*u+2,3)/2));}
function eout(u){return u<0?0:(u>1?1:1-Math.pow(1-u,3));}
function fog(z){return cl(0.5-(z-DIST)/(1.9*CP.gap),0,1);}
var INK='242,237,224',AMBER='239,159,39',DIM=1;
// ---- the pointer's spatial index: grains binned by last frame's screen position (a linked list per cell)
var CELL=32,GCW=1,GCH=1,HEAD=new Int32Array(64),NEXT=new Int32Array(NS),QL=new Int32Array(NS),QX=new Float32Array(NS),QY=new Float32Array(NS),QZ=new Float32Array(NS),
 QWX=new Float32Array(NS),QWY=new Float32Array(NS),QWZ=new Float32Array(NS);
function grid(){GCW=Math.ceil(w/CELL)+1;GCH=Math.ceil(h/CELL)+1;if(HEAD.length<GCW*GCH)HEAD=new Int32Array(GCW*GCH);HEAD.fill(-1);}
function bin(i){if(PX<0||PY<0||PX>=w||PY>=h)return;var c=Math.floor(PY/CELL)*GCW+Math.floor(PX/CELL);QX[i]=PX;QY[i]=PY;QZ[i]=PZ;QWX[i]=WX;QWY[i]=WY;QWZ[i]=WZ;NEXT[i]=HEAD[c];HEAD[c]=i;}
function query(x,y,R){var n=0,c0=Math.max(0,Math.floor((x-R)/CELL)),c1=Math.min(GCW-1,Math.floor((x+R)/CELL)),r0=Math.max(0,Math.floor((y-R)/CELL)),r1=Math.min(GCH-1,Math.floor((y+R)/CELL));
 for(var r=r0;r<=r1;r++)for(var c=c0;c<=c1;c++){for(var i=HEAD[r*GCW+c];i>=0;i=NEXT[i]){var dx=QX[i]-x,dy=QY[i]-y;if(dx*dx+dy*dy<R*R)QL[n++]=i;}}return n;}
// ---- grains: one pass computes each grain's place, layer (for occlusion) and brightness into a few buffers
var LAY=4,BUF=[],BN=new Int32Array(LAY*NB),TRL=[],TN=new Int32Array(LAY);
for(var bl=0;bl<LAY*NB;bl++)BUF.push(new Float32Array(NM*3));for(var tl_=0;tl_<LAY;tl_++)TRL.push(new Float32Array(NM*4));
var GA=0,GL=0,PH=0,PK=0,REM=0;
function putP(l,al,sz){var f=fog(PZ);var a=al*(0.3+0.7*f)*DIM;var b=Math.min(NB-1,Math.floor(a*NB));if(b<0)return;
 var s=cl(sz*PS*0.019,0.8,ZC);var o=BN[l*NB+b]++*3,B=BUF[l*NB+b];B[o]=PX-s/2;B[o+1]=PY-s/2;B[o+2]=s;}
function put(l,al,sz){if(al<=0.01||!prj(WX,WY,WZ))return;putP(l,al,sz);}
// a trail is short (at most `most` px): sand dropping through a mesh, never a long arc
function tpush(l,x0,y0,x1,y1,most){var dx=x0-x1,dy=y0-y1,n=Math.sqrt(dx*dx+dy*dy);if(n>most){x0=x1+dx*most/n;y0=y1+dy*most/n;}
 var o=TN[l]++*4,T=TRL[l];T[o]=x0;T[o+1]=y0;T[o+2]=x1;T[o+3]=y1;}
function trail(l,x0,y0,z0,x1,y1,z1){if(!prj(x1,y1,z1))return;var ax=PX,ay=PY;if(!prj(x0,y0,z0))return;tpush(l,PX,PY,ax,ay,10);}
function rest(k,i){spt(k,GU[k*NM+i],GV[k*NM+i],GH[k*NM+i]);}
// where grain i lies when nothing moves at stage st: sets WX..WZ, GA (brightness), GL (layer), PH/PK (at rest on
// sieve PK: the physics may move it); false: not drawn
function calm(i,st,t,pf){var n=cntAt(st);
 if(i<n){if(st>=4)return false;var k=levAt(st);rest(k,i);GL=k;GA=0.72;PH=1;PK=k;
  if(ACT===0&&st===0&&i>=NM-110){var q=(t*0.5+GD[i]*3)%1;if(q<0.72){var e=q/0.72;WY+=1.3*(1-e*e);GA=0.3+0.5*e;GL=0;PH=0;}}
  // the working sieve: what is read as bright as calm sand, the rest dimmer (never brighter: depth decides that)
  else if(ACT===1&&k===1)GA=i<pf*n?0.72:0.42;
  else if((ACT===2&&k===1)||(ACT===3&&k===2))GA=GV[k*NM+i]<-1+2*scanV(t,pf)?0.72:0.45;
  return true;}
 if(i>=N[1]){if(i<N[1]+RES){rest(0,i);GL=0;GA=0.2;PH=1;PK=0;REM=1;return true;}return false;}
 if(i>=N[2]){if(i<N[2]+RES){rest(1,i);GL=1;GA=0.2;PH=1;PK=1;REM=1;return true;}return false;}
 return false;}
// a grain falling from sieve ka to sieve kb (kb<0: through the last sieve into the dark): it falls first and glides
// sideways only a little (mostly straight down through the mesh)
var FX=0,FY=0,FZ=0,QA=[0,0,0],QB=[0,0,0];
function fpos(q){var e=q*q,m=Math.pow(q,2.3);FX=QA[0]+(QB[0]-QA[0])*m;FY=QA[1]+(QB[1]-QA[1])*e;FZ=QA[2]+(QB[2]-QA[2])*m;}
function fall(i,ka,kb,u,lay){var q=cl((u-GD[i])/0.6,0,1);rest(ka,i);QA[0]=WX;QA[1]=WY;QA[2]=WZ;
 if(kb<0)spt(2,GU[2*NM+i],GV[2*NM+i],-1.5);else rest(kb,i);QB[0]=WX;QB[1]=WY;QB[2]=WZ;
 fpos(q);WX=FX;WY=FY;WZ=FZ;GL=q>0?lay:ka;GA=kb<0?0.8*(1-q):0.8;
 if(q>0&&q<1){var x1=FX,y1=FY,z1=FZ;fpos(Math.max(0,q-0.1));trail(lay,FX,FY,FZ,x1,y1,z1);WX=x1;WY=y1;WZ=z1;}}
function moving(i,hp,t,pf){var b=hp.to,u=hp.u;
 if(b===1){if(i<N[1]){fall(i,0,1,u,1);return true;}rest(0,i);GL=0;GA=(i<N[1]+RES?0.78-0.58*ease(u):0.78*(1-ease(u)));return true;}
 if(b===2){if(!calm(i,1,t,pf))return false;if(i<N[1])GA=Math.min(1,GA+0.3*Math.sin(Math.PI*u));return true;}
 if(b===3){if(i<N[2]){fall(i,1,2,u,2);return true;}if(i<N[1]){rest(1,i);GL=1;GA=(i<N[2]+RES?0.78-0.58*ease(u):0.78*(1-ease(u)));return true;}return calm(i,3,t,pf);}
 if(b===4){if(i<NG)return false;if(i<N[2]){fall(i,2,-1,u,3);return true;}return calm(i,4,t,pf);}
 return calm(i,b,t,pf);}
// the physics' offset of grain i on top of its analytic place: a grain at rest on a sieve carries it (a new sieve
// starts it from zero); a grain the story moves (a rush, the rain on the first sieve) lets it fade quickly
function phys(i,dt){if(PH){if(SK[i]!==PK){SandCore.zero(SS,i);SK[i]=PK;}SS.hu[i]=GU[PK*NM+i];SS.hv[i]=GV[PK*NM+i];SS.ha[i]=SA[PK];}
 if(!SS.aw[i])return;if(!PH){var f=Math.exp(-10*dt);SS.x[i]*=f;SS.y[i]*=f;SS.z[i]*=f;SS.vx[i]*=f;SS.vy[i]*=f;SS.vz[i]*=f;}
 // a dim remnant the pointer stirs rises towards calm sand (0.4), so a furrow shows even where little sand is left
 if(REM)GA+=0.2*Math.min(1,(Math.abs(SS.x[i])+Math.abs(SS.y[i])+Math.abs(SS.z[i]))/0.05);
 WX+=SS.x[i];WY+=SS.y[i];WZ+=SS.z[i];GA*=SS.vis[i];if(SS.fall[i])GL=Math.min(3,GL+1);
 // a moving grain catches a little light (at most 1.35x); only a fast one (a shockwave's) leaves a short streak
 var v2=SS.vx[i]*SS.vx[i]+SS.vy[i]*SS.vy[i]+SS.vz[i]*SS.vz[i];GA=Math.min(1,GA*(1+Math.min(0.35,Math.sqrt(v2)*0.4)));if(v2>2.5&&GA>0.1){var k=0.035;trail(GL,WX-SS.vx[i]*k,WY-SS.vy[i]*k,WZ-SS.vz[i]*k,WX,WY,WZ);}}
function grains(t,st,hp,fl,pf,dt){BN.fill(0);TN.fill(0);var m=fl<0?1:ease((fl-0.42)/0.58);
 for(var i=0;i<NM;i++){PH=0;REM=0;var ok=hp?moving(i,hp,t,pf):calm(i,st,t,pf);
  if(fl>=0){if(!ok){GA=0;GL=0;WX=SX[i];WY=SY[i];WZ=SZ[i];}
   var tx=WX,ty=WY,tz=WZ;WX=SX[i]+(tx-SX[i])*m;WY=SY[i]+(ty-SY[i])*m;WZ=SZ[i]+(tz-SZ[i])*m;GA=0.5*(1-m)+GA*m;if(m<0.6)GL=0;
   // a streak: where the star was a moment ago (the camera flies along its view axis); only the near stars streak
   if(m<0.25&&i%3===0&&prj(WX,WY,WZ)&&PZ<2.4){var x1=PX,y1=PY,z2=PZ+(flyD(t-0.022)-flyD(t));if(z2>0.15)tpush(0,ox+XC*Fc/z2,oy-YC*Fc/z2,x1,y1,16);}
   put(GL,GA,GZ[i]*(1+0.8*(1-m)));continue;}
  if(!ok)continue;phys(i,dt);if(GA<=0.01||!prj(WX,WY,WZ))continue;if(PH)bin(i);putP(GL,GA,GZ[i]);}}
function flush(l){var lm=EMP[l>2?2:l];for(var b=0;b<NB;b++){var n=BN[l*NB+b];if(!n)continue;var B=BUF[l*NB+b];ctx.beginPath();
  for(var j=0;j<n*3;j+=3)ctx.rect(B[j],B[j+1],B[j+2],B[j+2]);ctx.fillStyle='rgba('+INK+','+Math.min(1,(b+0.55)/NB*lm).toFixed(3)+')';ctx.fill();}
 var tn=TN[l];if(tn){var T=TRL[l];ctx.beginPath();for(var q=0;q<tn*4;q+=4){ctx.moveTo(T[q],T[q+1]);ctx.lineTo(T[q+2],T[q+3]);}
  ctx.strokeStyle='rgba('+INK+','+Math.min(0.5,0.3*DIM*lm).toFixed(3)+')';ctx.lineWidth=1;ctx.stroke();}}
// ---- a sieve: a dark translucent plane (it veils what lies below it), a fine mesh fading with depth, and its rim.
// Where a plane above covers it, its mesh and its rim are both hidden (one rule: an overlap reads as one plane in front)
// A close camera can have part of a sieve behind it: the plane, its mesh and its rim are cut at the near plane (ZN)
// instead of vanishing. QD holds each plane's outline on the screen after the cut (at most 8 points, QN of them).
var SEG=[new Float32Array(400),new Float32Array(400),new Float32Array(400)],SN=[0,0,0],QD=new Float32Array(48),QN=[0,0,0],QOK=[0,0,0],ZN=0.16;
var CX=new Float32Array(9),CY=new Float32Array(9),CZ=new Float32Array(9);
function cam3(){var dx=WX-Cx,dy=WY-Cy,dz=WZ-Cz;XC=dx*rx+dy*ry+dz*rz;YC=dx*ux+dy*uy+dz*uz;PZ=dx*fx+dy*fy+dz*fz;}
function qput(k,x,y,z){var n=QN[k];if(n>=8)return;var s=Fc/z;QD[k*16+n*2]=ox+x*s;QD[k*16+n*2+1]=oy-y*s;QN[k]=n+1;}
function quads(){for(var k=0;k<3;k++){QN[k]=0;for(var c=0;c<4;c++){spt(k,c===1||c===2?1:-1,c>=2?1:-1,0);cam3();CX[c]=XC;CY[c]=YC;CZ[c]=PZ;}
 for(var c2=0;c2<4;c2++){var d=(c2+1)%4,a=CZ[c2]>=ZN,b=CZ[d]>=ZN;if(a)qput(k,CX[c2],CY[c2],CZ[c2]);
  if(a!==b){var u=(ZN-CZ[c2])/(CZ[d]-CZ[c2]);qput(k,CX[c2]+(CX[d]-CX[c2])*u,CY[c2]+(CY[d]-CY[c2])*u,ZN);}}
 QOK[k]=QN[k]>=3?1:0;}}
function qpath(k){var o=k*16,n=QN[k];ctx.moveTo(QD[o],QD[o+1]);for(var i=1;i<n;i++)ctx.lineTo(QD[o+i*2],QD[o+i*2+1]);ctx.closePath();}
// a world segment from the point last set by spt to (x1, y1, z1)'s camera coordinates, cut at the near plane: false
// when all of it lies behind; else A0/A1 its ends on the screen
var A0x=0,A0y=0,A1x=0,A1y=0,S0x=0,S0y=0,S0z=0;
function seg(x0,y0,z0,x1,y1,z1){if(z0<ZN&&z1<ZN)return false;if(z0<ZN){var u=(ZN-z0)/(z1-z0);x0+=(x1-x0)*u;y0+=(y1-y0)*u;z0=ZN;}
 else if(z1<ZN){var v=(ZN-z1)/(z0-z1);x1+=(x0-x1)*v;y1+=(y0-y1)*v;z1=ZN;}
 var s0=Fc/z0,s1=Fc/z1;A0x=ox+x0*s0;A0y=oy-y0*s0;A1x=ox+x1*s1;A1y=oy-y1*s1;PZ=(z0+z1)/2;return true;}
// the rim's progress: RP[k] the share done (1: the stage is done), RI[k] set while the amount is unknown (a short arc
// circles), RF[k] a brief brightening as the stage completes. The stroke starts at the near left corner and runs
// around the rim (the parameter s goes 0..4, one unit per edge)
var RP=[0,0,0],RI=[0,0,0],RF=[0,0,0],RI0=0,RC=[[-1,-1],[1,-1],[1,1],[-1,1]];
function rimW(k,s,hg){var e=Math.floor(s),f=s-e;e=((e%4)+4)%4;var c0=RC[e],c1=RC[(e+1)%4],r=RS[k];spt(k,(c0[0]+(c1[0]-c0[0])*f)*r,(c0[1]+(c1[1]-c0[1])*f)*r,hg===undefined?0.002:hg);}
function rimPt(k,s){rimW(k,s);return prj(WX,WY,WZ);}
function rimPath(k,s0,s1,hg){var s=s0,px_=0,py_=0,pz_=0,first=true;
 while(true){rimW(k,s,hg);cam3();var x1=XC,y1=YC,z1=PZ;if(!first&&seg(px_,py_,pz_,x1,y1,z1)){ctx.moveTo(A0x,A0y);ctx.lineTo(A1x,A1y);}px_=x1;py_=y1;pz_=z1;first=false;
  if(s>=s1)break;s=Math.min(s1,Math.floor(s)+1);}}
function rims(t,st,pv,hp){for(var k=0;k<3;k++){RI[k]=0;RF[k]=0;
  if(st>=SDONE[k]){RP[k]=1;continue;}RP[k]=0;
  if(hp&&SVOF[hp.to]===k){RP[k]=eout(hp.u/0.5);RF[k]=1-hp.u;continue;}
  if(ACT===null||hp)continue;var a=ACT,unk=FRAC===null;
  if(k===0&&a===0){if(unk)RI[k]=1;else RP[k]=pv;}
  else if(k===1&&(a===1||a===2)){if(unk)RI[k]=1;else RP[k]=a===1?0.15*pv:0.15+0.85*pv;}
  else if(k===2&&a===3){if(unk)RI[k]=1;else RP[k]=pv;}}
 for(var j=0;j<FLS.length;j++){var e=t-FLS[j].t;if(e>=0&&e<0.8)RF[FLS[j].k]=Math.max(RF[FLS[j].k],1-e/0.8);}
 RI0=REDUCED?0:(t*0.35)%4;}
// depth: each edge of a rim is dimmer the further it lies (EZ: 1 for the nearest edge, 0.38 for the furthest), and a
// hairline just under the rim gives the sieve a little thickness
var EZ=new Float32Array(4);
function edges(k){var z0=1e9,z1=-1e9;for(var e=0;e<4;e++){rimW(k,e+0.5);cam3();EZ[e]=PZ;if(PZ<z0)z0=PZ;if(PZ>z1)z1=PZ;}
 for(var e2=0;e2<4;e2++)EZ[e2]=1-0.62*(z1>z0?(EZ[e2]-z0)/(z1-z0):0);}
function rimStroke(k,s0,s1,al,lw,hg){for(var e=Math.floor(s0);e<s1;e++){var a=Math.max(s0,e),b=Math.min(s1,e+1);if(b-a<1e-4)continue;
 ctx.beginPath();rimPath(k,a,b,hg);ctx.strokeStyle='rgba('+INK+','+Math.min(0.95,al*EZ[((e%4)+4)%4]).toFixed(3)+')';ctx.lineWidth=lw;ctx.stroke();}}
var RMUL=1;
function rim(k){var lm=EMP[k]*DIM*RMUL,p=RP[k],done=p>=1;if(lm<0.01)return;edges(k);
 // the track: faint for a stage not done yet; a done stage keeps its whole rim, brighter (done reads apart from waiting)
 var tr=done?(0.56+0.25*RF[k]):Math.min(0.24,0.1+0.05*HL[k]);
 rimStroke(k,0,4,tr*lm,done?1.25:1);rimStroke(k,0,4,tr*0.4*lm,0.7,-0.035*SA[k]);
 if(done||(p<=0.001&&!RI[k]))return;
 var s0=RI[k]?RI0:0,s1=RI[k]?RI0+0.55:4*p;rimStroke(k,s0,s1,(0.78+0.25*RF[k])*lm,1.6);
 // the leading end of a stroke still filling: a small bright point
 if(!RI[k]&&rimPt(k,s1)){ctx.fillStyle='rgba('+INK+','+Math.min(1,0.95*lm).toFixed(3)+')';ctx.beginPath();ctx.arc(PX,PY,1.9,0,6.2832);ctx.fill();}}
function plane(k,hl,scan){if(!QOK[k])return;var fk=BR.f[k],ro=k?cl((fk-0.3)/0.5,0,1):0;
 // flat, the planes veil less (the sand of every stage shows through the one grid)
 ctx.beginPath();qpath(k);ctx.fillStyle='rgba(7,7,7,'+(0.62*(1-0.8*fk)).toFixed(3)+')';ctx.fill();
 ctx.save();for(var up=0;up<k;up++){if(!QOK[up])continue;ctx.beginPath();ctx.rect(-2,-2,w+4,h+4);qpath(up);try{ctx.clip('evenodd');}catch(e){}}
 SN[0]=SN[1]=SN[2]=0;var A=(0.14+0.2*hl)*DIM*EMP[k];
 for(var d=0;d<2;d++)for(var j=0;j<=GR;j++){var s=-1+2*j/GR;for(var q=0;q<3;q++){var v0=-1+2*q/3,v1=v0+2/3;
  if(d)spt(k,v0,s,0);else spt(k,s,v0,0);cam3();var x0=XC,y0=YC,z0=PZ;if(d)spt(k,v1,s,0);else spt(k,s,v1,0);cam3();if(!seg(x0,y0,z0,XC,YC,PZ))continue;
  var bi=Math.min(2,Math.floor(fog(PZ)*3));var o=SN[bi];if(o+4>400)continue;var S=SEG[bi];S[o]=A0x;S[o+1]=A0y;S[o+2]=A1x;S[o+3]=A1y;SN[bi]=o+4;}}
 for(var b=0;b<3;b++){if(!SN[b])continue;var S2=SEG[b];ctx.beginPath();for(var m=0;m<SN[b];m+=4){ctx.moveTo(S2[m],S2[m+1]);ctx.lineTo(S2[m+2],S2[m+3]);}
  ctx.strokeStyle='rgba('+INK+','+Math.min(0.5,A*(0.35+0.325*b)).toFixed(3)+')';ctx.lineWidth=b===2?1:0.7;ctx.stroke();}
 RMUL=1-ro;rim(k);RMUL=1;
 // the scan line of a stage at work: a thin line like the others (at most 0.35, 1 px)
 if(scan!==null){var vs=-1+2*scan;ctx.beginPath();for(var e=0;e<=8;e++){spt(k,-1+e/4,vs,0.004);if(prj(WX,WY,WZ)){if(e)ctx.lineTo(PX,PY);else ctx.moveTo(PX,PY);}}
  ctx.strokeStyle='rgba('+INK+','+Math.min(0.35,0.35*DIM*EMP[k]).toFixed(3)+')';ctx.lineWidth=1;ctx.stroke();}
 ctx.restore();
 // a lower sieve's nested rim, flat under the grid: drawn over the planes above it, fading in as it comes down
 if(ro>0){RMUL=ro;rim(k);RMUL=1;}}
function scanV(t,pf){return FRAC===null?0.5+0.5*Math.sin(t*0.9):pf;}
// ---- the final list: amber grains on the last sieve and one thin amber line (heavy grains: the pointer only makes
// them wobble); the grain a row of the panel points at (LIT) grows a little and is named beside it
var GX=new Float32Array(10),GY=new Float32Array(10),GS=new Float32Array(10),GAL=new Float32Array(10),GJ=new Int32Array(10),GN=0;
var GHW=new Float32Array(10),ANYH=0,LIT=-1;
function goldTime(){if(!TR||TR.to<4)return -1e9;return TR.t+(4-TR.from)*HOP;}
var GLK=0;
function gold(t,st,hp){var n=0;var g4=st>=4,gh=hp&&hp.to===4;GN=0;if(!g4&&!gh)return 0;
 for(var j=0;j<NG;j++){var q=gh?eout((hp.u-0.2)/0.8):1;rest(2,j);var ax=WX,ay=WY,az=WZ;spt(2,GLU[j],GLV[j],0.03);
  var o=NM+j;WX=ax+(WX-ax)*q+SS.x[o];WY=ay+(WY-ay)*q+SS.y[o];WZ=az+(WZ-az)*q+SS.z[o];if(!prj(WX,WY,WZ))continue;
  GX[n]=PX;GY[n]=PY;GS[n]=cl(0.03*PS,1.4,3.2*cl(Math.sqrt(D0/DIST),1,1.8))*(1+0.3*GHW[j]);GAL[n]=0.35+0.65*q;GJ[n]=j;bin(o);n++;}
 GLK=eout((t-goldTime())/0.55);GN=n;goldDraw(1);return n;}
// the amber line and grains as placed by gold() (m: their weight). Flat, the sieves above lie on the same plane and
// would veil them: frame() draws them once more on top, so the final list stays at least as bright as in the stack
function goldDraw(m){var n=GN,lk=GLK;if(m<=0.01)return;
 if(n>1&&lk>0){var tot=(n-1)*lk;ctx.beginPath();ctx.moveTo(GX[0],GY[0]);for(var s=1;s<n;s++){if(s<=tot)ctx.lineTo(GX[s],GY[s]);else{var f=tot-(s-1);if(f>0)ctx.lineTo(GX[s-1]+(GX[s]-GX[s-1])*f,GY[s-1]+(GY[s]-GY[s-1])*f);break;}}
  ctx.strokeStyle='rgba('+AMBER+','+(0.8*m).toFixed(3)+')';ctx.lineWidth=1;ctx.stroke();}
 for(var k=0;k<n;k++){ctx.fillStyle='rgba('+AMBER+','+(GAL[k]*m).toFixed(3)+')';ctx.beginPath();ctx.arc(GX[k],GY[k],GS[k],0,6.2832);ctx.fill();}}
var MONO='ui-monospace,SFMono-Regular,Menlo,Consolas,monospace';
var SANS='-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei","Noto Sans CJK SC",sans-serif';
function tw(s){var m=ctx.measureText(s);return (m&&m.width)||0;}
function spacing(v){try{ctx.letterSpacing=v;}catch(e){}}
function fmtN(v){v=Math.round(v);var s=String(Math.abs(v)),o='';while(s.length>3){o=','+s.slice(-3)+o;s=s.slice(0,-3);}return (v<0?'-':'')+s+o;}
// a sieve's corner furthest right (or left) on the screen: where its numbers and its tooltip sit
function corner(k,left){var bx=left?1e9:-1e9,by=0;for(var c=0;c<4;c++){spt(k,c&1?1:-1,c&2?1:-1,0);if(prj(WX,WY,WZ)&&(left?PX<bx:PX>bx)){bx=PX;by=PY;}}return [bx,by];}
// words or numbers beside a point on a dark plate (no mesh line runs through them); on the other side when the
// free part of the scene ends first; always inside it (a 16 px gutter)
function plate(s,x,y,al,font,sp,alt){ctx.font=font;spacing(sp);var n=tw(s);if(x+n>w-INS[0]-16&&alt!==undefined)x=alt-n;
 x=cl(x,16,Math.max(16,w-INS[0]-16-n));y=cl(y,26,Math.max(26,h-INS[1]-16));ctx.fillStyle='rgba(7,7,7,'+(0.72*al).toFixed(3)+')';ctx.fillRect(x-4,y-11,n+8,15);
 ctx.fillStyle='rgba('+INK+','+(0.8*al).toFixed(3)+')';ctx.fillText(s,x,y);spacing('0px');}
// a sieve's corner nearest the camera (the lowest on the screen)
function nearC(k){var bx=0,by=-1e9;for(var c=0;c<4;c++){spt(k,c&1?1:-1,c&2?1:-1,0);if(prj(WX,WY,WZ)&&PY>by){bx=PX;by=PY;}}return [bx,by];}
var FLS=[];if(TR){for(var fs_=TR.from+1;fs_<=TR.to;fs_++){var fk=SVOF[fs_];if(fk>=0)FLS.push({k:fk,t:TR.t+(fs_-TR.from-1)*HOP+HOP*0.35});}}
function flashing(t){for(var i=0;i<FLS.length;i++){var e=t-FLS[i].t;if(e>-0.1&&e<FLD+0.05)return true;}return false;}
// a stage that completes on this page shows its real numbers under its rim's nearest corner for ~2.6 s, rolling up,
// then they fade: the instrument's small letter-spaced monospace at its low opacity, kept inside the scene
function flashes(t){for(var i=0;i<FLS.length;i++){var F=FLS[i],e=t-F.t,n=NUM[F.k];if(e<0||e>FLD||n[0]===null||!QOK[F.k])continue;
 var al=Math.min(1,e/0.25)*Math.min(1,(FLD-e)/0.6),u=eout(e/RD),s=fmtN(n[0]*u)+(n[1]!==null?' / '+fmtN(n[1]):'');
 var c=nearC(F.k);ctx.textBaseline='alphabetic';ctx.font='10px '+MONO;spacing('0.14em');var tw0=tw(s);
 var x=cl(c[0]-tw0/2,16,Math.max(16,w-INS[0]-16-tw0)),y=cl(c[1]+20,26,Math.max(26,h-INS[1]-16));
 ctx.fillStyle='rgba(7,7,7,'+(0.55*al).toFixed(3)+')';ctx.fillRect(x-4,y-10,tw0+8,14);
 ctx.fillStyle='rgba('+INK+','+(0.6*al).toFixed(3)+')';ctx.fillText(s,x,y);spacing('0px');}}
// the grain a row of the panel points at: its short name (or its ticker) beside it
function litName(){if(LIT<0)return;for(var q=0;q<GN;q++){if(GJ[q]!==LIT)continue;var p=PICKS[LIT]||[],nm=String(p[0]||p[1]||'');if(!nm)return;
 var cjk=/[㐀-鿿]/.test(nm);ctx.textBaseline='alphabetic';plate(nm,GX[q]+10,GY[q]+4,GHW[LIT],(cjk?'':'500 ')+'11px '+(p[0]?SANS:MONO),cjk||!p[0]?'0px':'0.04em',GX[q]-10);return;}}
// damped values (no tweens): the highlight of each sieve, a slight camera lean towards the working one, and the
// emphasis of the sieve pointed at (it brightens, the others dim)
var HL=[0,0,0],EMP=[1,1,1],lastT=null;
function damp(v,g,k,dt){return v+(g-v)*(1-Math.exp(-k*dt));}
function stars(){for(var j=0;j<BG;j++){if(!prj(BX[j],BY[j],BZ[j]))continue;var a=(0.07+0.16*rnd(j*3.7))*DIM;ctx.fillStyle='rgba('+INK+','+a.toFixed(3)+')';ctx.fillRect(PX,PY,1,1);}}
// ---- the pointer: position, a damped velocity, a short trail (the wake), shockwaves, what it points at
var UI={x:0,y:0,cx:0,cy:0,on:false,down:false,drag:false,mode:'',btn:0,id:null,sx:0,sy:0,lx:0,ly:0,lt:0,vx:0,vy:0,touch:false},BUSY=0;
var LASTUSE=-1e9,HOV=-1,HG=-1,PINJ=-1,TIPT=0,CAND=-1,CANDT=0,LASTEV=0,TRX=new Float32Array(12),TRY=new Float32Array(12),TRT=new Float64Array(12),TRN=0,SHK=[],SHN=0,ACC=0,HSTEP=1/120;
var PT={},PN=0,PIN=null,LASTTAP=0,LTX=0,LTY=0,FOCK=-1;
var PR=46,PFORCE=1500,SSPD=1000,SRAD=170,SIMP=1.5;
function physOn(){return !REDUCED;}
// a use of the scene: the page's refresh waits (window.__jevSandBusy); a press, the wheel or a key also stop the
// breathing (the window's listeners see them too; a move stops it only with intent, see intent())
function poke(){BUSY=Date.now();try{W.__jevSandBusy=BUSY;}catch(e){}}
function busy(ms){return Date.now()-BUSY<ms;}
// while the page breathes, a tooltip shows only after a use with intent (a hand resting on the mouse over the grid
// never stops the breathing through a tooltip)
function mayHover(){return REDUCED||!SandBreath.active(BR)||clock()-LASTUSE<1500;}
// the pointer's ray through the screen point (x, y), and where it crosses the level of a grain: the push is along the
// ground, away from that point (a grain lies on a sieve), with a little lift
var RDX=0,RDY=0,RDZ=0,HX=0,HZ=0;
function ray(x,y){var a=(x-ox)/Fc,b=(y-oy)/Fc;RDX=fx+rx*a-ux*b;RDY=fy+ry*a-uy*b;RDZ=fz+rz*a-uz*b;}
function away(i){var t=(QWY[i]-Cy)/Math.min(RDY,-1e-3);HX=QWX[i]-(Cx+RDX*t);HZ=QWZ[i]-(Cz+RDZ*t);
 if(HX*HX+HZ*HZ<1e-8){var an=rnd(i*1.7)*6.2832;HX=Math.cos(an)*1e-3;HZ=Math.sin(an)*1e-3;}}
// the field around one point of the pointer's path (s: its strength): grains near the ray are pushed away from it
// along the ground (nearer grains more), and along the cursor's motion, a little lifted, when it moves (the wake)
function field(x,y,s,hs){if(s<=0)return;var n=query(x,y,PR);if(!n)return;var sp=Math.sqrt(UI.vx*UI.vx+UI.vy*UI.vy),mv=Math.min(1,sp/900),gx=0,gz=0;
 if(mv>0.02){ray(x,y);var t0=-Cy/Math.min(RDY,-1e-3),hx0=Cx+RDX*t0,hz0=Cz+RDZ*t0;ray(x+UI.vx/sp*20,y+UI.vy/sp*20);var t1=-Cy/Math.min(RDY,-1e-3);
  gx=Cx+RDX*t1-hx0;gz=Cz+RDZ*t1-hz0;var gl=Math.sqrt(gx*gx+gz*gz)||1;gx/=gl;gz/=gl;}
 ray(x,y);
 for(var q=0;q<n;q++){var i=QL[q];if(i>=NM&&(mv<0.05||i-NM===HG))continue;   // a pick only wobbles as the cursor passes, never slips from under it
  // a grain that fell through and came back is left alone by a still cursor (no endless fountain at a sieve's edge)
  if(i<NM&&SS.sp[i]){if(mv<0.05||SS.vis[i]<1)continue;SS.sp[i]=0;}
  var dx=QX[i]-x,dy=QY[i]-y,d=Math.sqrt(dx*dx+dy*dy);away(i);var hl=Math.sqrt(HX*HX+HZ*HZ);
  var z=QZ[i],nr=cl(D0/z,0.5,1.6),a=PFORCE*s*nr*nr*z/Fc;
  SandCore.radial(SS,i,HX/hl+0.7*mv*gx,0,HZ/hl+0.7*mv*gz,1-d/PR,a,0.12+0.35*mv,hs);}}
function shocks(now){for(var k=SHK.length-1;k>=0;k--){var S=SHK[k],r=(now-S.t)/1000*SSPD;if(r>SRAD+60){if(now-S.t>800)SHK.splice(k,1);continue;}
 var n=query(S.x,S.y,Math.min(SRAD,r));if(!n)continue;ray(S.x,S.y);for(var q=0;q<n;q++){var i=QL[q];if(SID[i]===S.id)continue;SID[i]=S.id;
  var dx=QX[i]-S.x,dy=QY[i]-S.y,d=Math.sqrt(dx*dx+dy*dy);away(i);var nr=cl(D0/QZ[i],0.5,1.6);
  SandCore.radial(SS,i,HX,0,HZ,1-d/SRAD,SIMP*nr,0.8,0);}}}
// the fixed-step integration (120 Hz, at most 4 steps a frame): the pointer, its wake, the shockwaves, then the sand
function physics(dt){if(!physOn())return;ACC=Math.min(ACC+Math.min(dt,1/15),0.05);var now=clock(),n=0;
 while(ACC>=HSTEP&&n<4){
  // a pointer that stopped (no event for 40 ms) stops pushing along its old motion: its velocity dies away
  if(now-UI.lt>40){var dk=Math.exp(-14*HSTEP);UI.vx*=dk;UI.vy*=dk;}
  if(UI.on&&!UI.drag)field(UI.x,UI.y,1,HSTEP);
  for(var k=0;k<TRN;k++){var age=(now-TRT[k])/1000;if(age>0.02&&age<0.3)field(TRX[k],TRY[k],0.25*(1-age/0.3),HSTEP);}
  if(SHK.length)shocks(now);SandCore.step(SS,HSTEP);ACC-=HSTEP;n++;}}
// a sieve under the point (x, y): the plane in front wins (the top one is drawn last)
function inQuad(k,x,y){if(!QOK[k])return false;var o=k*16,sg=0,n=QN[k];for(var c=0;c<n;c++){var c2=(c+1)%n,x0=QD[o+c*2],y0=QD[o+c*2+1],x1=QD[o+c2*2],y1=QD[o+c2*2+1];
 var cr=(x1-x0)*(y-y0)-(y1-y0)*(x-x0);if(cr!==0){var s=cr>0?1:-1;if(sg&&s!==sg)return false;sg=s;}}return true;}
function hit(x,y){var best=-1,bd=196;for(var j=0;j<GN;j++){var dx=GX[j]-x,dy=GY[j]-y,d=dx*dx+dy*dy;if(d<bd){bd=d;best=j;}}
 if(best>=0)return {g:GJ[best]};for(var k=0;k<3;k++)if(inQuad(k,x,y))return {k:k};return null;}
var TIPEL=null,TIPON=false,TIPX=-1,TIPY=-1;
function tipEl(){if(!TIPEL&&document.createElement&&document.body&&document.body.appendChild){TIPEL=document.createElement('div');TIPEL.className='sandtip';
 try{TIPEL.setAttribute('aria-hidden','true');}catch(e){}document.body.appendChild(TIPEL);}return TIPEL;}
// an amber grain: its rank, short name and ticker, and a quiet hint how to open its evidence
function tipText(hh){if(!hh)return '';if(hh.g!==undefined){var p=PICKS[hh.g]||[];var nm=String(p[0]||''),tk=String(p[1]||''),rk=p[2];
 var s=nm&&tk?nm+' · '+tk:(nm||tk);return (typeof rk==='number'?'#'+rk+' ':'')+s;}return TIP[hh.k]||'';}
// where a tooltip sits: beside the sieve's rim (its right corner) or beside the grain, in the free part of the scene
function tipAt(){var x=0,y=0,r=cv.getBoundingClientRect?cv.getBoundingClientRect():{left:0,top:0};
 if(HG>=0){for(var q=0;q<GN;q++)if(GJ[q]===HG){x=GX[q]+12;y=GY[q]-26;}}else if(HOV>=0){var c=corner(HOV);x=c[0]+10;y=c[1]-24;}
 return [x+(r.left||0),y+(r.top||0)];}
function placeTip(){var el=TIPEL;if(!el||!TIPON)return;var p=tipAt(),vw=(W.innerWidth||800)-INS[0],vh=(W.innerHeight||600)-INS[1],tw_=el.offsetWidth||120,th=el.offsetHeight||22,x=p[0],y=p[1];
 if(x+tw_>vw-16)x=p[0]-tw_-24;if(y<16)y=p[1]+34;x=Math.round(cl(x,16,Math.max(16,vw-16-tw_)));y=Math.round(cl(y,16,Math.max(16,vh-16-th)));
 if(x!==TIPX||y!==TIPY){TIPX=x;TIPY=y;el.style.left=x+'px';el.style.top=y+'px';}}
function showTip(s,hint){var el=tipEl();if(!el)return;if(!s){el.className='sandtip';TIPON=false;return;}el.textContent=s;el.className='sandtip on';TIPON=true;TIPX=TIPY=-1;
 try{if(hint)el.setAttribute('data-hint',hint);else if(el.removeAttribute)el.removeAttribute('data-hint');}catch(e){}placeTip();}
function rankOf(j){var p=PICKS[j];return p&&typeof p[2]==='number'?p[2]:null;}
function setHover(hh){var h0=HOV,g0=HG;HOV=hh&&hh.k!==undefined?hh.k:-1;HG=hh&&hh.g!==undefined?hh.g:-1;
 try{cv.style.cursor=HG>=0?'pointer':(UI.drag?(UI.mode==='pan'?'move':'grabbing'):'grab');}catch(e){}
 // the hint: a mouse clicks; a finger taps a second time (the first tap pinned this tooltip)
 var hint=HG>=0&&rankOf(HG)!==null?(UI.touch?(PINJ===HG?TIPAGAIN:''):TIPCLICK):'';
 showTip(tipText(hh),hint);if(HG!==g0&&HUD&&HUD.hover){try{HUD.hover(HG>=0?rankOf(HG):null);}catch(e){}}
 if(REDUCED&&(HOV!==h0||HG!==g0))redraw();}
function unpin(){if(TIPT)clearTimeout(TIPT);TIPT=0;}
function pin(){unpin();TIPT=setTimeout(function(){TIPT=0;PINJ=-1;if(!UI.on||UI.touch)setHover(null);},2600);}
// a click on an amber grain opens that company's row (its evidence) in the results panel and brings it into view
function toRow(j){var rk=rankOf(j);if(rk===null)return false;if(HUD&&HUD.pick){try{if(HUD.pick(rk))return true;}catch(e){}}
 var d=document.getElementById('r'+rk);if(!d)return false;
 try{if(!d.open)d.open=true;}catch(e){}
 try{d.scrollIntoView({behavior:REDUCED?'auto':'smooth',block:'center'});}catch(e){try{d.scrollIntoView();}catch(e2){}}
 try{var c0=String(d.className||'').replace(/\s*sandlit/g,'');d.className=c0+' sandlit';setTimeout(function(){d.className=String(d.className).replace(/\s*sandlit/g,'');},1600);}catch(e){}
 return true;}
// the ring a click leaves on the sieve it hit: where the pointer's ray meets that plane
function onPlane(k,x,y){var dx=fx+rx*(x-ox)/Fc-ux*(y-oy)/Fc,dy=fy+ry*(x-ox)/Fc-uy*(y-oy)/Fc,dz=fz+rz*(x-ox)/Fc-uz*(y-oy)/Fc,g=G[k];
 var nx=g.ay*g.bz-g.az*g.by,ny=g.az*g.bx-g.ax*g.bz,nz=g.ax*g.by-g.ay*g.bx,dn=dx*nx+dy*ny+dz*nz;if(Math.abs(dn)<1e-6)return null;
 var t=((g.x-Cx)*nx+(g.y-Cy)*ny+(g.z-Cz)*nz)/dn;if(t<=0)return null;var X=Cx+dx*t-g.x,Y=Cy+dy*t-g.y,Z=Cz+dz*t-g.z;
 return [(X*g.ax+Y*g.ay+Z*g.az)/g.a,(X*g.bx+Y*g.by+Z*g.bz)/g.a];}
function shock(x,y,hh){SHN=SHN%65535+1;var s={x:x,y:y,t:clock(),id:SHN,k:-1,u:0,v:0};
 if(hh&&hh.k!==undefined){var uv=onPlane(hh.k,x,y);if(uv){s.k=hh.k;s.u=uv[0];s.v=uv[1];}}SHK.push(s);if(SHK.length>4)SHK.shift();}
function rings(){var now=clock();for(var k=0;k<SHK.length;k++){var S=SHK[k],e=(now-S.t)/700;if(S.k<0||e>=1)continue;var r=0.08+0.5*eout(e);
 ctx.beginPath();for(var c=0;c<=40;c++){var an=c/40*6.2832;spt(S.k,S.u+r*Math.cos(an),S.v+r*Math.sin(an),0.002);if(prj(WX,WY,WZ)){if(c)ctx.lineTo(PX,PY);else ctx.moveTo(PX,PY);}}
 ctx.strokeStyle='rgba('+INK+','+(0.34*(1-e)*(1-e)).toFixed(3)+')';ctx.lineWidth=1;ctx.stroke();}}
function click(x,y,touch){var hh=hit(x,y);
 // a first tap only names the company; a second tap (or a click) opens its row
 if(hh&&hh.g!==undefined){if(touch&&PINJ!==hh.g){PINJ=hh.g;setHover(hh);pin();return;}PINJ=-1;toRow(hh.g);return;}
 if(touch){setHover(hh);if(hh)pin();}
 if(physOn())shock(x,y,hh);}
// ---- the view: fly to a sieve (a double-click or Enter), reset (a double-click on empty space, Esc, the HUD's button)
function kick(){if(!id&&!REDUCED&&!PAUSED&&!document.hidden)id=W.requestAnimationFrame(tick);}
function moved(){if(REDUCED){SandCam.snap(CAM);redraw();}else kick();}
// (to the sieve's resting place: the breathing, cancelled by this, takes the sieves back there)
function flyTo(k){var c=CP.sc[k];handover();SandCam.fly(CAM,c[0],c[1]-FOCD,c[2],D0*0.42);poke();moved();}
function resetView(){SandCam.reset(CAM);PART[0]=PART[1]=0;poke();moved();}
// the point a zoom keeps under the pointer: where its ray meets the sieve under it, else the depth of the look-at point
function anchor(x,y){var hh=hit(x,y);ray(x,y);var t=DIST;
 if(hh&&hh.k===undefined&&hh.g!==undefined)hh={k:2};
 if(hh){var g=G[hh.k],nx=g.ay*g.bz-g.az*g.by,ny=g.az*g.bx-g.ax*g.bz,nz=g.ax*g.by-g.ay*g.bx,dn=RDX*nx+RDY*ny+RDZ*nz;
  if(Math.abs(dn)>1e-6){var tt=((g.x-Cx)*nx+(g.y-Cy)*ny+(g.z-Cz)*nz)/dn;if(tt>0.2)t=tt;}}
 return [Cx+RDX*t,Cy+RDY*t-FOCD,Cz+RDZ*t];}
function zoomAt(f,x,y){var p=anchor(x,y);SandCam.zoomAbout(CAM,f,p[0],p[1],p[2]);moved();}
function loc(e){var r=cv.getBoundingClientRect?cv.getBoundingClientRect():box.getBoundingClientRect();UI.cx=e.clientX;UI.cy=e.clientY;UI.x=e.clientX-(r.left||0);UI.y=e.clientY-(r.top||0);}
function pinchPts(){var o=[];for(var k in PT){if(Object.prototype.hasOwnProperty.call(PT,k))o.push(PT[k]);}return o.length>=2?o:null;}
function pinchNow(){var p=pinchPts();if(!p)return null;var dx=p[0][0]-p[1][0],dy=p[0][1]-p[1][1];return {d:Math.max(10,Math.sqrt(dx*dx+dy*dy)),x:(p[0][0]+p[1][0])/2,y:(p[0][1]+p[1][1])/2};}
// two fingers: their spread zooms (about their midpoint) and their midpoint pans
function pinchMove(){var q=pinchNow();if(!q||!PIN){PIN=q;return;}var p=anchor(q.x,q.y);SandCam.zoomAbout(CAM,PIN.d/q.d,p[0],p[1],p[2]);SandCam.pan(CAM,q.x-PIN.x,q.y-PIN.y,Fc,BS);PIN=q;moved();}
function onMove(e){if(UI.down&&e.pointerType==='mouse'&&e.buttons===0&&e.pointerId===UI.id){onCancel(e);}   // a release we never saw
 loc(e);var now=clock(),dt=Math.max(1,now-UI.lt)/1000;poke();LASTEV=Date.now();
 if(PT[e.pointerId])PT[e.pointerId]=[UI.x,UI.y];
 if(UI.mode==='pinch'){pinchMove();return;}
 if(!UI.down&&e.pointerType)UI.touch=e.pointerType==='touch'||e.pointerType==='pen';
 if(UI.lt&&dt<0.2){UI.vx=damp(UI.vx,(UI.x-UI.lx)/dt,18,dt);UI.vy=damp(UI.vy,(UI.y-UI.ly)/dt,18,dt);}else{UI.vx=0;UI.vy=0;}
 var ddx=UI.x-UI.lx,ddy=UI.y-UI.ly;UI.lx=UI.x;UI.ly=UI.y;UI.lt=now;
 TRX[TRN%12]=UI.x;TRY[TRN%12]=UI.y;TRT[TRN%12]=now;TRN=Math.min(12,TRN+1);if(TRN===12){for(var k=0;k<11;k++){TRX[k]=TRX[k+1];TRY[k]=TRY[k+1];TRT[k]=TRT[k+1];}TRN=11;}
 if(UI.down&&e.pointerId===UI.id){var mx=UI.x-UI.sx,my=UI.y-UI.sy;
  if(!UI.drag&&mx*mx+my*my>36){UI.drag=true;UI.mode=UI.btn===2?'pan':'orbit';try{cv.setPointerCapture(e.pointerId);}catch(e2){}setHover(null);}
  if(UI.drag){if(UI.mode==='pan')SandCam.pan(CAM,ddx,ddy,Fc,BS);else SandCam.orbit(CAM,ddx,ddy,dt);moved();return;}}
 if(!UI.touch){UI.on=true;PART[0]=REDUCED?0:(UI.x/w-0.5)*2*PARY;PART[1]=REDUCED?0:(UI.y/h-0.5)*2*PARP;}
 if(!TIPT||!UI.touch){var hh=hit(UI.x,UI.y);if(!hh||hh.g!==PINJ){unpin();PINJ=-1;}
  // a sieve tells its numbers only once the pointer rests on it (~350 ms, slower than ~150 px/s): playing with the sand
  // and reading never fight; an amber grain, a tap, the keys and reduced motion answer at once
  if(hh&&hh.k!==undefined&&physOn()&&!UI.touch){var fast=UI.down||Math.sqrt(UI.vx*UI.vx+UI.vy*UI.vy)>150;
   if(hh.k!==CAND||fast){CAND=hh.k;CANDT=now;}
   if(HOV!==hh.k||fast){if(HOV>=0||HG>=0)setHover(null);}else if(mayHover())setHover(hh);}
  else{CAND=-1;if(!hh||mayHover())setHover(hh);}}
 kick();}
function onDown(e){UI.touch=e.pointerType==='touch'||e.pointerType==='pen';loc(e);LASTEV=Date.now();CAND=-1;poke();LASTUSE=clock();handover();FOCK=-1;
 if(!PT[e.pointerId])PN++;PT[e.pointerId]=[UI.x,UI.y];
 if(PN>=2){UI.mode='pinch';UI.drag=true;UI.down=false;PIN=pinchNow();setHover(null);try{cv.setPointerCapture(e.pointerId);}catch(e2){}kick();return;}
 if(e.button>2)return;UI.btn=(e.button===2||e.button===1||(e.button===0&&e.shiftKey))?2:0;
 if(HOV>=0&&!UI.touch)setHover(null);UI.down=true;UI.drag=false;UI.mode='';UI.id=e.pointerId;UI.sx=UI.x;UI.sy=UI.y;
 try{cv.setPointerCapture(e.pointerId);}catch(e2){}   // the release comes back here wherever it happens
 UI.lx=UI.x;UI.ly=UI.y;UI.lt=clock();UI.on=true;CAM.vy=0;CAM.vp=0;kick();}
// a release after holding still flings nothing: the glide fades with the time since the last move
function release(){var idle=(clock()-UI.lt)/1000;if(idle>0.06||REDUCED){CAM.vy=CAM.vp=0;}else{var k=Math.exp(-12*idle);CAM.vy*=k;CAM.vp*=k;}}
function lift(e){if(PT[e.pointerId]){delete PT[e.pointerId];PN=Math.max(0,PN-1);}}
function onUp(e){lift(e);if(UI.mode==='pinch'){if(PN<2){UI.mode='';UI.drag=false;UI.down=false;PIN=null;UI.id=null;}poke();return;}
 if(!UI.down||e.pointerId!==UI.id)return;loc(e);var dragged=UI.drag;UI.down=false;UI.drag=false;UI.mode='';poke();LASTEV=Date.now();if(dragged)release();
 try{cv.releasePointerCapture(e.pointerId);}catch(e2){}
 if(!dragged&&UI.btn===0){var nw=clock(),dx=UI.x-LTX,dy=UI.y-LTY;
  // a double tap (a finger) flies like a double-click
  if(UI.touch&&nw-LASTTAP<320&&dx*dx+dy*dy<900){LASTTAP=0;dbl(UI.x,UI.y);}else{LASTTAP=nw;LTX=UI.x;LTY=UI.y;click(UI.x,UI.y,UI.touch);}}
 if(UI.touch){UI.on=false;UI.vx=UI.vy=0;}
 try{cv.style.cursor=HG>=0?'pointer':'grab';}catch(e3){}}
function onCancel(e){if(e)lift(e);if(UI.drag&&UI.mode!=='pinch')release();UI.down=false;UI.drag=false;UI.mode='';PIN=null;if(UI.touch)UI.on=false;}
function onLeave(e){if(e&&e.pointerType&&e.pointerType!=='mouse')return;UI.on=false;UI.vx=UI.vy=0;PART[0]=PART[1]=0;CAND=-1;if(!TIPT)setHover(null);}
// (the sieve whose tooltip shows wins: the sieves may still be gliding back from the breathing's flat grid)
function dbl(x,y){var hh=hit(x,y);if(hh&&hh.g!==undefined)return;if(HOV>=0)hh={k:HOV};if(hh)flyTo(hh.k);else resetView();}
function onDbl(e){loc(e);try{e.preventDefault();}catch(e2){}dbl(UI.x,UI.y);}
// the wheel (a trackpad pinch arrives as a wheel with ctrlKey) zooms towards the pointer; the page never scrolls here
function onWheel(e){try{e.preventDefault();}catch(e2){}loc(e);poke();LASTUSE=clock();handover();LASTEV=Date.now();var dy=Number(e.deltaY)||0;
 if(e.deltaMode===1)dy*=16;else if(e.deltaMode===2)dy*=400;setHover(null);CAND=-1;zoomAt(Math.exp(cl(dy,-240,240)*(e.ctrlKey?0.01:0.0015)),UI.x,UI.y);}
// the keys on the focused scene: up/down step through the sieves and the amber grains (their tooltip shows), Enter
// flies to a sieve or opens a grain's row, left/right orbit, + and - zoom, 0 or Esc reset
function onKey(e){var k=e.key,n=3+GN;
 if(k==='ArrowDown'||k==='ArrowUp'){FOCK=FOCK<0?(k==='ArrowDown'?0:n-1):(FOCK+(k==='ArrowDown'?1:n-1))%n;UI.touch=false;setHover(FOCK<3?{k:FOCK}:{g:GJ[FOCK-3]});}
 else if(k==='Enter'||k===' '){if(FOCK>=0&&FOCK<3)flyTo(FOCK);else if(FOCK>=3)toRow(GJ[FOCK-3]);else return;}
 else if(k==='ArrowLeft'||k==='ArrowRight'){SandCam.orbit(CAM,k==='ArrowLeft'?-40:40,0,0);moved();}
 else if(k==='+'||k==='='){zoomAt(0.8,(w-INS[0])/2,(h-INS[1])/2);}else if(k==='-'||k==='_'){zoomAt(1.25,(w-INS[0])/2,(h-INS[1])/2);}
 else if(k==='0'||k==='Escape'||k==='Home'){FOCK=-1;setHover(null);resetView();}
 else return;
 poke();LASTUSE=clock();handover();LASTEV=Date.now();try{e.preventDefault();}catch(e2){}}
function settled(){var e=Math.abs(PAR[0]-PART[0])+Math.abs(PAR[1]-PART[1]);
 for(var k=0;k<3;k++)e+=Math.abs(EMP[k]-(HOV<0?1:(k===HOV?1.35:0.45)));for(var j=0;j<10;j++)e+=Math.abs(GHW[j]-((HG===j||LIT===j)?1:0));
 return e+Math.abs(ANYH-(HOV>=0?1:0))<2e-4;}
// ---- one frame
var FIN=false;
function frame(t){if(PAUSED)return;var t0=clock();var dt=lastT===null?1:cl(t-lastT,0,0.25);lastT=t;ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);
 var st=reached(t),hp=hopAt(t),fl=flyAt(t),pv=pf(t),dts=Math.min(dt,1/15);DIM=1;insets(dts);
 // the breathing: a rush (a stage's shake and numbers, the opening, the gold's arrival) takes priority; anyone using
 // the scene (a press, a drag, a tooltip, a key's focus, the view still gliding) holds it off
 var tgd=t-goldTime();SandBreath.step(BR,dts,{reduced:REDUCED,rush:!!hp||fl>=0||flashing(t)||(tgd>=-0.2&&tgd<2),
  busy:PAUSED||UI.down||UI.drag||HOV>=0||HG>=0||LIT>=0||FOCK>=0||!!TIPT||!SandCam.settled(CAM)});
 var work=levAt(st);for(var k=0;k<3;k++){var g=STATE==='run'&&k===work?0.5:(st>=4&&k===2?0.35:0.12);if(hp&&SVOF[hp.to]===k)g=1;HL[k]=damp(HL[k],g,hp?14:9,dt);
  var eg=HOV<0?1:(k===HOV?1.35:0.45);EMP[k]=REDUCED?eg:damp(EMP[k],eg,10,dts);}
 for(var gw=0;gw<10;gw++){var gg=(HG===gw||LIT===gw)?1:0;GHW[gw]=REDUCED?gg:damp(GHW[gw],gg,12,dts);}ANYH=REDUCED?(HOV>=0?1:0):damp(ANYH,HOV>=0?1:0,10,dts);
 FOC=damp(FOC,STATE==='run'?-0.06*work:0,4,dt);
 if(REDUCED){SandCam.snap(CAM);}else{SandCam.step(CAM,dts,UI.drag);PAR[0]=damp(PAR[0],PART[0],3.5,dts);PAR[1]=damp(PAR[1],PART[1],3.5,dts);}
 ZC=2.4*cl(Math.sqrt(D0/Math.max(0.5,CAM.c)),1,1.35);
 camAt(t);sieves(t);if(fl<0)physics(dt);grid();quads();rims(t,st,pv,hp);
 stars();grains(t,st,hp,fl,pv,dts);flush(3);
 var sc2=(!hp&&ACT===3)?scanV(t,pv):null,sc1=(!hp&&ACT===2)?scanV(t,pv):null;
 plane(2,HL[2],sc2);flush(2);gold(t,st,hp);
 plane(1,HL[1],sc1);flush(1);plane(0,HL[0],null);flush(0);
 var gw=cl((Math.max(BR.f[0],BR.f[1],BR.f[2])-0.3)/0.5,0,1);if(gw>0&&GN)goldDraw(gw);
 rings();flashes(t);litName();
 if(TIPON)placeTip();
 if(!FIN&&TR&&TR.to===4&&t-goldTime()>1.6){FIN=true;if(HUD&&HUD.finished){try{HUD.finished();}catch(e){}}}
 box.__ms=clock()-t0;box.__n=(box.__n||0)+1;}
function pf(t){if(FRAC===null)return 0;if(PFRAC===null||PFRAC===undefined)return FRAC;return PFRAC+(FRAC-PFRAC)*eout((t-ROLL0)/RD);}
function T(){return T0+(clock()-perf0)/1000;}
function save(){try{var t=T();var o={t:t,at:Date.now(),stage:STAGE,f:FRAC,a:ACT};if(rushing(t))o.tr={from:TR.from,to:TR.to,at:Date.now()-(t-TR.t)*1000};
 var cs=SandCam.state(CAM);if(cs){for(var k in cs)if(Object.prototype.hasOwnProperty.call(cs,k))o[k]=cs[k];}
 if(PART[0]||PART[1]){o.px=Math.round(PART[0]*1e4)/1e4;o.py=Math.round(PART[1]*1e4)/1e4;}
 if(!REDUCED){o.br=SandBreath.state(BR);if(LOGEL)o.log=SandLog.state(LG);}
 W.sessionStorage.setItem(KEY,JSON.stringify(o));}catch(e){}}
save();
// the first drawing is made now, before any animation frame (no empty scene, even when frames never come)
var lastDraw=T0,id=0,CAP=1/30-0.004;frame(T0);logRows();
// the breathing stops for any use of the page, anywhere on it: a press, the wheel, a key (the camera handed over at
// once) or a pointer moved with intent (more than ~40 px within a quarter second, or a slow move ~10 px away from
// where it rested: a hand resting on the mouse, a pixel or two of jitter, is not a use; the lift glides back)
var IM=0,IMX=null,IMY=0,IMT=0,IAX=null,IAY=0;
function used(){LASTUSE=clock();handover();}
function moveUse(){LASTUSE=clock();SandBreath.poke(BR);}
function intent(e){var x=Number(e.clientX)||0,y=Number(e.clientY)||0,n=clock();
 if(IMX!==null){IM=IM*Math.exp(-Math.max(0,n-IMT)/250)+Math.sqrt((x-IMX)*(x-IMX)+(y-IMY)*(y-IMY));if(IM>40){IM=0;IAX=x;IAY=y;moveUse();}}
 if(IAX===null||(x-IAX)*(x-IAX)+(y-IAY)*(y-IAY)>=100){if(IAX!==null)moveUse();IAX=x;IAY=y;}IMX=x;IMY=y;IMT=n;}
try{var PO={passive:true,capture:true};W.addEventListener('pointermove',intent,PO);W.addEventListener('pointerdown',used,PO);
 W.addEventListener('wheel',used,PO);W.addEventListener('keydown',used,PO);W.addEventListener('touchstart',used,PO);}catch(e){}
function redraw(){lastT=null;frame(REDUCED?T0:T());}
if(cv.addEventListener){cv.addEventListener('pointermove',onMove);cv.addEventListener('pointerdown',onDown);cv.addEventListener('pointerup',onUp);
 cv.addEventListener('pointercancel',onCancel);cv.addEventListener('pointerleave',onLeave);cv.addEventListener('dblclick',onDbl);
 cv.addEventListener('wheel',onWheel,{passive:false});cv.addEventListener('keydown',onKey);
 cv.addEventListener('contextmenu',function(e){try{e.preventDefault();}catch(e2){}});
 cv.addEventListener('blur',function(){if(FOCK>=0){FOCK=-1;setHover(null);}});
 // a press that lost its pointer (captured elsewhere, the window left while held) ends like a cancel
 cv.addEventListener('lostpointercapture',function(e){if(UI.down&&e.pointerId===UI.id&&UI.mode!=='pinch')onCancel(e);});
 W.addEventListener('blur',function(){if(UI.down||UI.drag)onCancel(null);});}
// a tooltip being read with a still mouse (or a pinned one) keeps the page's refresh waiting (window.__jevSandBusy),
// for at most 15 s after the last pointer event (a mouse left on the scene never blocks the updates)
setInterval(function(){if(document.hidden||PAUSED)return;if(((UI.on&&(HOV>=0||HG>=0||CAND>=0))||TIPT||FOCK>=0)&&Date.now()-LASTEV<15000)poke();},500);
// the HUD's handle
function light(rank){var j=-1;for(var q=0;q<PICKS.length;q++)if(PICKS[q]&&PICKS[q][2]===rank&&rank!==null&&rank!==undefined){j=q;break;}
 if(j===LIT)return;LIT=j;if(REDUCED)redraw();else kick();}
function pause(p){p=!!p;if(p===PAUSED)return;PAUSED=p;if(p){if(id)W.cancelAnimationFrame(id);id=0;setHover(null);save();}
 else{size();lastT=null;LGT=0;var t=T();frame(t);lastDraw=t;kick();}}
W.__jevSand={light:light,reset:resetView,pause:pause,relayout:function(){INT=hudInset();if(REDUCED){INS=INT.slice();fit();redraw();}else kick();}};
W.addEventListener('pagehide',save);W.addEventListener('beforeunload',save);setInterval(save,1000);
if(REDUCED){W.addEventListener('resize',function(){size();redraw();});return;}
// a failed run (nothing moves on) redraws at 15 fps; 60 while someone plays, the sand moves, the view settles, during
// the opening, a rush, a stage's numbers and the gold's arrival; 30 otherwise
function every(t){if(busy(1500)||SS.moving>0||SHK.length||!settled()||!SandCam.settled(CAM)||Math.abs(INS[0]-INT[0])+Math.abs(INS[1]-INT[1])>0.3)return 0;
 if(STATE==='fail')return 1/15;if(hopAt(t)||flyAt(t)>=0)return 0;var tg=goldTime();if(t-tg>=0&&t-tg<1.4)return 0;return flashing(t)?1/45:CAP;}
function tick(){id=0;if(PAUSED)return;var t=T();logTick(clock());if(CAND>=0&&HOV!==CAND&&UI.on&&!UI.down&&!UI.drag&&clock()-CANDT>=350&&mayHover())setHover({k:CAND});
 if(t-lastDraw>=every(t)){frame(t);lastDraw=t;}if(UI.down||UI.drag)poke();
 if(!document.hidden)id=W.requestAnimationFrame(tick);}
kick();
document.addEventListener('visibilitychange',function(){LGT=0;if(document.hidden){if(id)W.cancelAnimationFrame(id);id=0;save();}else if(!id&&!PAUSED){lastT=null;id=W.requestAnimationFrame(tick);}});
W.addEventListener('resize',function(){if(PAUSED)return;size();var t=T();frame(t);lastDraw=t;});
}catch(e){try{if(!window.__jevSand){var sb=document.getElementById('sand');if(sb)sb.className='sand';}}catch(e2){}}})();
"""
