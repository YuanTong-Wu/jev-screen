"""The HUD of the one page (owner, 2026-09-28): while JavaScript runs, the page is ONE full-window 3D sand scene
(jevscreen.page_sand) with a restrained, instrument-like overlay, instead of checklist and progress cards beside a
separate drawing.

- Top left: the idea as a faint title; under it ONE small status dot for every prerequisite: green when all are
  ready, red with one plain sentence and its fix when something is wrong (a check that needs the user, a block, a
  stopped worker), a quiet breathing dot while the checks are still running. Hovering, tapping or focusing the dot
  opens the whole checklist (the page's own ready section, cloned).
- Top right: a tiny instrument (small letter-spaced monospace numbers at low opacity): the cost so far and the cap,
  the time (ticking while the worker runs), the stage (n / 3, done, waiting, stopped).
- The results live in a panel on the right (a bottom sheet on a phone) that scrolls on its own: the page's own
  results and scope questions; it opens by itself once the list is done (after the gold links up when the finish is
  watched live) and remembers being closed. The amber grains and the rows light each other both ways.
- Bottom left: a "文字视图 / Text view" toggle: the full plain page (checklist, progress bars, questions, results) for
  screen readers, printing and the user's AI (it is also the page without JavaScript, `#text` in the address, and
  what gets printed). `jevscreen page --text` is unchanged. Bottom right: a small reset-view control.

Every word is built here in the page's one language (zh fully Chinese, en fully English). Deterministic: no clock is
read here (the page's script ticks the time from the worker's start). Inline only (the page's CSP).
"""
from __future__ import annotations

import html as _html
from typing import Any

STRINGS = {
    "zh": {"tv": "文字视图", "tv_title": "完整的文字页面（读屏、打印、给你的 AI）", "back": "立体视图",
           "reset": "重置视角", "controls": "滚轮缩放 · 拖动旋转 · 右键拖动平移 · 双击筛子飞近",
           "results_n": "结果 · {n} 家", "results_main": "结果 · 确认 {m} 家",
           "results_split": "结果 · 确认 {m} 家 · 待核对 {u}", "results": "结果", "questions": "待你回答", "hide": "收起结果",
           "show": "展开结果", "cost": "合计", "cost_title": "这个任务的合计花费 / 花费上限（含验证 key；结果面板里的"
           "「花费」只算这一版）", "time": "用时", "stage": "阶段", "done": "完成", "wait": "等待", "prep": "准备 · 0 / 3",
           "fail": "已停", "checks": "准备情况", "busy": "准备中", "all_set": "准备都好了", "legend": "图例",
           "expand": "展开或收起结果"},
    "en": {"tv": "Text view", "tv_title": "The full page as text (screen readers, printing, your AI)",
           "back": "3D view", "reset": "Reset view",
           "controls": "Wheel to zoom · drag to orbit · right-drag to pan · double-click a sieve to fly in",
           "results_n": "Results · {n}", "results_main": "Results · {m} confirmed",
           "results_split": "Results · {m} confirmed · {u} to confirm", "results": "Results", "questions": "Questions for you",
           "hide": "Hide results", "show": "Show results", "cost": "Total",
           "cost_title": "This job's total spend / its cap (includes the key check; the results panel's cost is "
                         "this version's only)",
           "time": "Time", "stage": "Stage", "done": "Done", "wait": "Waiting", "prep": "Prep · 0 / 3", "fail": "Stopped", "checks": "Checks",
           "busy": "Getting ready", "all_set": "All set", "legend": "Legend", "expand": "Expand or collapse the results"},
}


def _lang(data: dict[str, Any]) -> str:
    return "en" if data.get("lang") == "en" else "zh"


def status(live: dict[str, Any] | None, lang: str) -> dict[str, Any] | None:
    """The status dot: {state: ok | bad | run | wait, text, fix}; None without a status block. `bad` carries the ONE
    sentence shown beside the dot (a block or stop first, then the first check that needs the user or failed); `ok`
    once every prerequisite is ready (also while the screening itself runs); `run` / `wait` only while a
    prerequisite is still being prepared."""
    if not isinstance(live, dict):
        return None
    S = STRINGS["en" if lang == "en" else "zh"]
    for a in live.get("alerts") or []:
        if isinstance(a, dict) and a.get("kind") != "note" and a.get("text"):
            return {"state": "bad", "text": str(a["text"]), "fix": None}
    checks = [c for c in live.get("checks") or [] if isinstance(c, dict)]
    for c in checks:
        if c.get("state") in ("need", "fail"):
            return {"state": "bad", "text": str(c.get("text") or ""), "fix": c.get("fix")}
    if live.get("all_ok"):
        return {"state": "ok", "text": str(live.get("ok_line") or S["checks"]), "fix": None}
    # every prerequisite is ready and the work itself runs (the last check, 'ready'): green, not a grey 'unknown'
    pre = [c for c in checks if c.get("id") != "ready"]
    work = next((c for c in checks if c.get("id") == "ready"), None)
    if pre and all(c.get("state") in ("ok", "opt") for c in pre) and work and work.get("state") == "run":
        return {"state": "ok", "text": S["all_set"] + (" · " + str(work["text"]) if work.get("text") else ""),
                "fix": None}
    run = next((c for c in checks if c.get("state") == "run"), None)
    if run:
        return {"state": "run", "text": str(run.get("text") or S["busy"]), "fix": None}
    wait = next((c for c in checks if c.get("state") == "wait"), None)
    return {"state": "wait", "text": str((wait or {}).get("text") or S["busy"]), "fix": None}


def _usd(v: float) -> str:
    return f"${v:.2f}" if v >= 0.01 or v == 0 else f"${v:.4f}"


def clock_words(seconds: float | None) -> str:
    """'04:31', '1:02:07'; '—' when unknown."""
    if seconds is None:
        return "—"
    s = max(0, int(seconds))
    h, m, x = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{x:02d}" if h else f"{m:02d}:{x:02d}"


def instrument(data: dict[str, Any]) -> dict[str, Any]:
    """The top-right instrument's readings: cost ('$0.18 / $1.00'), time (a finished run's duration; a running
    worker's start in `since`, ticked by the page), stage ('2 / 3', done, waiting, stopped)."""
    lang = _lang(data)
    S = STRINGS[lang]
    live = data.get("live") or {}
    hud = live.get("hud") or {}
    f = live.get("sand") or {}
    usd = hud.get("usd")
    if usd is None and data.get("run_id") and isinstance(data.get("cost_usd"), (int, float)):
        usd = float(data["cost_usd"])
    cap = hud.get("cap")
    cost = "—" if usd is None else _usd(float(usd)) + (f" / {_usd(float(cap))}" if cap else "")
    state = f.get("state") or "wait"
    since = hud.get("since") if state == "run" else None
    secs = data.get("seconds") if state in ("done", "fail") and data.get("run_id") else None
    if state == "run" and f.get("active") is not None:
        stage = f"{ {0: 1, 1: 2, 2: 2, 3: 3}.get(int(f['active']), 1) } / 3"
    elif state == "run":
        stage = S["prep"]       # novice #4 P1-3: the profile fill / the estimate run: not 'waiting'
    else:
        stage = {"done": S["done"], "fail": S["fail"]}.get(state, S["wait"])
    return {"cost": cost, "since": since, "time": clock_words(secs), "stage": stage,
            "phase": live.get("phase_words") or "", "doing": doing_words(live) if state == "run" else ""}


def doing_words(live: dict[str, Any]) -> str:
    """The one line under the instrument while the work runs: the running progress item ('补缺简介的公司 412 / 1,111'),
    else the status pill's words. Empty when nothing runs."""
    for it in ((live.get("progress") or {}).get("items") or []):
        if isinstance(it, dict) and it.get("state") == "run" and it.get("label"):
            return str(it["label"]) + (" · " + str(it["text"]) if it.get("text") else "")
    return str(live.get("phase_words") or "")


def panel(data: dict[str, Any]) -> tuple[bool, bool, str]:
    """(has a panel, open by default, its head's words): the results once there are some, else the scope questions
    when there are some; open by default when the list is done or questions wait."""
    S = STRINGS[_lang(data)]
    q = data.get("questions") or {}
    asks = bool(q.get("items") or q.get("notes"))
    done = ((data.get("live") or {}).get("sand") or {}).get("state") == "done"
    if data.get("run_id"):
        if data.get("shortlist"):          # the confirmed list and the to-confirm section: both counts, apart
            from .page import main_and_confirm
            m, u = (len(x) for x in main_and_confirm(data))
            return True, bool(done or asks), S["results_split" if u else "results_main"].format(m=m, u=u)
        return True, bool(done or asks), S["results_n"].format(n=len(data.get("rows") or []))
    if asks:
        return True, True, S["questions"]
    return False, False, S["results"]


RESET_SVG = ('<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false">'
             '<path d="M3.3 8.6A4.8 4.8 0 1 0 4.7 4.6" fill="none" stroke="currentColor" stroke-width="1.2" '
             'stroke-linecap="round"/><path d="M4.9 1.9v2.9H2" fill="none" stroke="currentColor" stroke-width="1.2" '
             'stroke-linecap="round" stroke-linejoin="round"/></svg>')


def markup(data: dict[str, Any]) -> str:
    """The HUD's elements (shown by the page's script only when the scene runs; empty when the page has no scene)."""
    live = data.get("live") or {}
    if not isinstance(live.get("sand"), dict):
        return ""
    lang = _lang(data)
    S = STRINGS[lang]
    e = lambda s: _html.escape(str(s), quote=True)  # noqa: E731
    st = status(live, lang) or {"state": "wait", "text": S["checks"], "fix": None}
    inst = instrument(data)
    has, opened, head = panel(data)
    title = data.get("headline") or data.get("idea") or ""
    bad = st["state"] == "bad"
    say = (f'<span class="say">{e(st["text"])}</span>' + (f'<span class="fix">{e(st["fix"])}</span>' if st.get("fix")
                                                            else "")) if bad else ""
    label = st["text"] + ((" " + str(st["fix"])) if bad and st.get("fix") else "")
    attrs = {"key": data.get("idea_key") or "", "lang": lang, "panel": "1" if has else "0",
             "open": "1" if opened else "0", "since": inst["since"] or "", "w-fail": S["fail"], "w-legend": S["legend"]}
    a = " ".join(f'data-{k}="{e(v)}"' for k, v in attrs.items())
    return (
        f'<div id="hud" class="hud" {a}>\n'
        f'<button type="button" class="hud-tv" id="hud-tv" aria-pressed="false" title="{e(S["tv_title"])}">'
        f'{e(S["tv"])}</button>\n'
        f'<div class="hud-title" aria-hidden="true">{e(title)}</div>\n'
        f'<div class="hud-stat s-{st["state"]}" id="hud-stat" tabindex="0" role="button" aria-expanded="false" '
        f'aria-controls="hud-pop" aria-label="{e(S["checks"] + ": " + label if lang == "en" else S["checks"] + "：" + label)}">'
        f'<span class="dot" aria-hidden="true"></span>{say}</div>\n'
        f'<div class="hud-pop" id="hud-pop" hidden></div>\n'
        f'<div class="hud-inst" title="{e(inst["phase"])}">'
        f'<div title="{e(S["cost_title"])}"><span class="k">{e(S["cost"])}</span>'
        f'<span class="v" id="hud-cost">{e(inst["cost"])}</span></div>'
        f'<div><span class="k">{e(S["time"])}</span><span class="v" id="hud-time">{e(inst["time"])}</span></div>'
        f'<div><span class="k">{e(S["stage"])}</span><span class="v" id="hud-stage">{e(inst["stage"])}</span></div>'
        + (f'<div class="doing" id="hud-doing">{e(inst["doing"])}</div>' if inst["doing"] else "") +
        f'</div>\n'
        f'<button type="button" class="hud-reset" id="hud-reset" aria-label="{e(S["reset"])}" '
        f'title="{e(S["reset"] + " · " + S["controls"])}">{RESET_SVG}</button>\n'
        f'<button type="button" class="hud-tab" id="hud-tab" aria-controls="app" title="{e(S["show"])}" hidden>'
        f'{e(head)}</button>\n'
        f'<button type="button" class="hud-back" id="hud-back">{e(S["back"])}</button>\n'
        f'<div class="hud-ph" id="hud-ph"><button type="button" class="t" id="hud-pt" aria-controls="app" '
        f'aria-expanded="{"true" if opened else "false"}" title="{e(S["expand"])}">{e(head)}</button>'
        f'<button type="button" class="hud-x" id="hud-hide" aria-label="{e(S["hide"])}" title="{e(S["hide"])}">'
        f'<span aria-hidden="true">×</span></button></div>\n'
        f'</div>\n')


_MONO = 'ui-monospace,SFMono-Regular,Menlo,Consolas,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",monospace'
_SANS = ('-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",'
         '"Noto Sans CJK SC",sans-serif')
# the page's own colour settings, defined once in page.py (TOKENS_DARK / TOKENS_LIGHT): repeated on the panel and the
# checklist pop-up, and printing from the 3D view takes the plain page's light palette whatever the screen showed
from .page import TOKENS_DARK, TOKENS_LIGHT  # noqa: E402  (page imports this module only inside render_page)
_DARK = TOKENS_DARK + ";color:var(--fg)"
_LIGHT = TOKENS_LIGHT

# The panel's own skin (owner review, 2026-09-28): not the plain page's cards inside a drawer but the HUD's quiet
# type: the head says "结果 · 3 家", then the ranked rows (rank, name, code, verdict) in the HUD's scale; the plain
# page's duplicate heading, version line, top table and evidence heading are left to the text view; the tags are
# thin chips told apart by their words and line style as on the text view (inference amber, a gap dashed); the
# legend sits behind a small "图例" disclosure; the personal-use note is one muted line at the foot. The whole panel skin is screen-only: printing gets the plain page's light palette.
P = "html.hud-on:not(.hud-text) #app>main"
CSS = ("""
.hud{display:none}.hud [hidden]{display:none!important}
html.hud-on,html.hud-on body{height:100%;overflow:hidden;background:#070707}
html.hud-on .hud{display:block}
html.hud-text,html.hud-text body{height:auto;overflow:visible;background:var(--bg)}
.hud>*{position:fixed;z-index:20}
.hud button{min-height:0;min-width:0;font:inherit;cursor:pointer}
.hud-title{left:16px;top:14px;max-width:calc(100vw - 46px - var(--hud-iw,184px) - var(--hud-pr,0px));font:500 13px/1.4 SANS;
color:rgba(var(--ink-rgb),.46);letter-spacing:.02em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
pointer-events:none;transition:max-width .35s ease}
.hud-stat{left:11px;top:36px;display:flex;align-items:baseline;flex-wrap:wrap;gap:2px 8px;padding:3px 5px;
border-radius:6px;max-width:min(620px,calc(100vw - 51px - var(--hud-iw,184px) - var(--hud-pr,0px)));cursor:pointer;
font:12px/1.45 SANS;color:rgba(var(--ink-rgb),.62);outline:none}
.hud-stat .dot{flex:0 0 7px;width:7px;height:7px;border-radius:50%;background:rgba(var(--ink-rgb),.42);
transform:translateY(-1px)}
.hud-stat.s-ok .dot{background:var(--ok);box-shadow:0 0 6px rgba(114,192,122,.55)}
.hud-stat.s-bad .dot{background:var(--bad);box-shadow:0 0 8px rgba(229,83,75,.65)}
.hud-stat.s-run .dot{animation:hudbreath 2.4s ease-in-out infinite}
@keyframes hudbreath{50%{opacity:.35}}
.hud-stat .say{color:#f1b3ad}.hud-stat .fix{color:rgba(var(--ink-rgb),.5)}
.hud-stat:focus-visible,.hud button:focus-visible{box-shadow:0 0 0 1px rgba(var(--ink-rgb),.4);outline:none}
.hud-pop{left:12px;top:64px;width:min(480px,calc(100vw - 24px));max-height:calc(100vh - 150px);overflow:auto;
padding:10px 14px;border-radius:10px;background:rgba(13,13,12,.96);border:1px solid rgba(var(--ink-rgb),.12);
box-shadow:0 12px 40px rgba(0,0,0,.5);font:13.5px/1.5 SANS;DARK}
.hud-pop section.box{margin:0;padding:0;border:0;background:transparent}.hud-pop h2{font-size:.9rem}
.hud-pop .alert{margin:0 0 8px}
.hud-inst{right:calc(16px + var(--hud-pr,0px));top:13px;font:10px/1.75 MONO;letter-spacing:.14em;
color:rgba(var(--ink-rgb),.4);text-align:right;transition:right .35s ease;font-variant-numeric:tabular-nums}
.hud-inst .k{margin-right:10px;color:rgba(var(--ink-rgb),.26);text-transform:uppercase}
.hud-inst .v{color:rgba(var(--ink-rgb),.62);text-transform:uppercase}
.hud-inst .doing{position:absolute;right:0;top:100%;max-width:min(70vw,340px);overflow:hidden;text-overflow:ellipsis;
white-space:nowrap;letter-spacing:.04em;color:rgba(var(--ink-rgb),.5)}
.hud-tv{left:10px;bottom:calc(10px + var(--hud-pb,0px));padding:8px 6px;border:0;border-radius:6px;
background:transparent;font:10px/1.2 MONO!important;letter-spacing:.14em;text-transform:uppercase;
color:rgba(var(--ink-rgb),.36);transition:bottom .35s ease}
.hud-tv:hover{color:rgba(var(--ink-rgb),.7)}
.hud-reset{right:calc(14px + var(--hud-pr,0px));bottom:calc(14px + var(--hud-pb,0px));width:30px;height:30px;
padding:0;display:flex;align-items:center;justify-content:center;border-radius:50%;
border:1px solid rgba(var(--ink-rgb),.14);background:rgba(7,7,7,.55);color:rgba(var(--ink-rgb),.5);
transition:right .35s ease,bottom .35s ease}
.hud-reset:hover{color:rgba(var(--ink-rgb),.85);border-color:rgba(var(--ink-rgb),.3)}
.hud-tab{right:0;top:50%;transform:translateY(-50%);writing-mode:vertical-rl;padding:14px 7px;border:1px solid
rgba(var(--ink-rgb),.12);border-right:0;border-radius:8px 0 0 8px;background:rgba(13,13,12,.85);
font:10px/1 MONO!important;letter-spacing:.16em;text-transform:uppercase;color:rgba(var(--ink-rgb),.6)}
.hud-back{display:none}.hud-ph{display:none}details.hud-lg{display:none}
@media (pointer:coarse){.hud-stat::before{content:"";position:absolute;left:-12px;top:-12px;right:-12px;bottom:-12px}
.hud-reset{width:40px;height:40px}}
html.hud-text .hud{display:flex;justify-content:flex-end;max-width:980px;margin:0 auto;padding:12px 16px 0}
html.hud-text .hud>*{display:none}
html.hud-text .hud>.hud-back{display:block;position:static;padding:6px 12px;min-height:36px;border-radius:2px;
border:1px solid var(--line2);background:transparent;color:var(--fg2);font:12px/1.2 SANS;letter-spacing:.06em}
html.hud-text .hud>.hud-back:hover{color:var(--fg);border-color:var(--line2)}
@media screen{
P{position:fixed;z-index:30;top:0;right:0;bottom:0;left:auto;width:clamp(320px,31vw,430px);max-width:none;margin:0;
padding:0 18px 28px;overflow-y:auto;overscroll-behavior:contain;font:13px/1.5 SANS;background:rgba(10,10,9,.94);
border-left:1px solid rgba(var(--ink-rgb),.08);transform:translateX(102%);visibility:hidden;
transition:transform .35s cubic-bezier(.2,.8,.2,1),visibility 0s .35s;DARK}
html.hud-on.hud-panel.hud-open:not(.hud-text) #app>main{transform:none;visibility:visible;
transition:transform .35s cubic-bezier(.2,.8,.2,1),visibility 0s}
html.hud-on:not(.hud-panel):not(.hud-text) #app>main{display:none}
P>header,html.hud-on:not(.hud-text) #app section.ready,html.hud-on:not(.hud-text) #app section.progress{display:none}
P>.hud-ph{display:flex;position:sticky;top:0;z-index:3;align-items:center;justify-content:space-between;gap:10px;
margin:0 -18px 4px;padding:12px 12px 10px 18px;background:#0a0a09;border-bottom:1px solid rgba(var(--ink-rgb),.07)}
P>.hud-ph .t{flex:1 1 auto;min-width:0;padding:0;border:0;background:transparent;text-align:left;cursor:default;
font:10.5px/1.4 MONO!important;letter-spacing:.14em;text-transform:uppercase;color:rgba(var(--ink-rgb),.62)}
P{display:flex;flex-direction:column}
P>*{flex:0 0 auto}
P>.hud-ph{order:-3}
P>section.results{order:-2}
P section.results{display:flex;flex-direction:column}
P section.results>*{order:5}
P section.results>h2:first-child,P section.results>h2:first-child+div.small,P section.results>h2:has(+.tablewrap),
P section.results>.tablewrap,P section.results>h2:has(+details.row),P details.row .badge{display:none}
P details.row{order:1;background:transparent;border:0;border-bottom:1px solid rgba(var(--ink-rgb),.07);border-radius:0;margin:0}
P details.row>summary{display:flex;flex-wrap:nowrap;align-items:baseline;gap:10px;padding:9px 2px}
P details.row>summary::after{content:"+";position:static;margin-left:auto;font:11px MONO;color:rgba(var(--ink-rgb),.28);font-weight:400}
P details.row[open]>summary::after{content:"\\2212"}
P .rank{flex:0 0 24px;font:10.5px MONO;letter-spacing:.06em;color:rgba(var(--ink-rgb),.4);font-weight:400}
P .row .name{flex:1 1 auto;min-width:min(9em,45%);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500;
color:rgba(var(--ink-rgb),.9)}
P .row summary>.muted.small{flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font:10.5px MONO;letter-spacing:.05em;color:rgba(var(--ink-rgb),.38)}
P .row summary>.small:not(.muted){flex:0 0 auto;font-size:12px;color:rgba(var(--ink-rgb),.62)}
P details.row .body{padding:0 2px 12px 34px;color:rgba(var(--ink-rgb),.78)}
P section.results>h2.mainh,P section.results>p.mainnote,P section.secondary>p.small{display:none}
P section.results>p.mainhint{display:block;order:0;margin:10px 0 2px;font-size:11.5px;color:rgba(var(--ink-rgb),.45)}
P section.results>p.empty{order:0;margin:12px 0 6px;padding:0;border:0;background:transparent;font-size:12.5px;
color:rgba(var(--ink-rgb),.62)}
P section.results>section.secondary{order:2;margin:18px 0 0;padding:0;border:0;background:transparent}
P section.secondary>h2{margin:0 0 2px;padding:10px 2px 0;border-top:1px solid rgba(var(--ink-rgb),.07);
font:10.5px/1.5 MONO;letter-spacing:.1em;color:rgba(var(--ink-rgb),.42)}
P section.secondary .rank{display:block;font-size:0}
P section.secondary .rank::before{content:"\\00b7";font:12px MONO;color:rgba(var(--ink-rgb),.28)}
P section.secondary .row .name{font-weight:400;color:rgba(var(--ink-rgb),.62)}
P section.secondary .row summary>.small:not(.muted){color:rgba(var(--ink-rgb),.42)}
P details.row>summary:has(>.badge.via){flex-wrap:wrap;row-gap:2px}
P details.row>summary:has(>.badge.via)>.name{flex:1 1 0;min-width:5em}
P details.row>summary:has(>.badge.via)::after{order:10;margin-left:auto}
P details.row .badge.via{display:inline;order:9;flex:0 0 auto;margin:0 0 0 34px;padding:0 5px;border:1px solid rgba(var(--ink-rgb),.16);border-radius:4px;
background:transparent;font:10px/1.6 MONO;letter-spacing:.03em;color:rgba(var(--ink-rgb),.55)}
P details.unv{order:2;margin:10px 0 0;font-size:12px;color:rgba(var(--ink-rgb),.55)}
P section.results>p:not(.small):not(.empty){order:3;margin:14px 0 4px;font-size:12px;color:rgba(var(--ink-rgb),.6)}
P section.results>div.small.muted{order:4;font:10.5px/1.6 MONO;letter-spacing:.04em;color:rgba(var(--ink-rgb),.38)}
P section.results>p.small{font-size:11.5px;color:rgba(var(--ink-rgb),.45)}
P h2{font-size:12px;font-weight:500;letter-spacing:.03em;color:rgba(var(--ink-rgb),.7);margin:14px 0 6px}
P .tag{border-color:rgba(var(--ink-rgb),.2);color:rgba(var(--ink-rgb),.6);font:10.5px/1.5 MONO;letter-spacing:.03em;padding:0 5px}
P .tag.t-gap{color:rgba(var(--ink-rgb),.72);border-color:rgba(var(--ink-rgb),.45)}P table.top10 td.t-gap{color:rgba(var(--ink-rgb),.72)}
P .tag.t-inference{color:var(--infer);border-color:var(--infer)}
P blockquote{border-left-color:rgba(var(--ink-rgb),.18)}P blockquote.gapq{border-left-color:rgba(var(--ink-rgb),.4)}
P a,P button.more{color:rgba(var(--ink-rgb),.82)}P a{text-decoration-color:rgba(var(--ink-rgb),.3)}
P details.hud-lg{display:block;order:5;margin:12px 0 0}
P details.hud-lg>summary{cursor:pointer;font:10px/1.6 MONO;letter-spacing:.14em;text-transform:uppercase;color:rgba(var(--ink-rgb),.4)}
P .legend{display:none;font-size:11.5px;color:rgba(var(--ink-rgb),.55)}P details.hud-lg[open]+.legend{display:flex}
P section.results>.banner{order:8;margin:16px 0 0;padding:10px 0 0;border:0;border-top:1px solid rgba(var(--ink-rgb),.07);border-radius:0;
font-size:11px;line-height:1.5;color:rgba(var(--ink-rgb),.42)}
P section.results>.banner::before{content:"";position:static;display:inline-block;width:1px;height:.9em;margin:0 8px 0 0;
border-radius:0;background:rgba(var(--ink-rgb),.6);vertical-align:-1px}
P section.results>footer{order:9;margin:14px 0 4px;font:10px/1.5 MONO;letter-spacing:.06em;color:rgba(var(--ink-rgb),.3)}
P section.box{background:transparent;border:0;padding:0;margin:8px 0}
P button{min-height:36px;border-color:rgba(var(--ink-rgb),.18);background:transparent;color:rgba(var(--ink-rgb),.85);font-size:13px}
P button.on{background:rgba(var(--ink-rgb),.88);color:#111;border-color:transparent}
.hud-x{width:28px;height:28px;padding:0;border:0;border-radius:6px;background:transparent;color:rgba(var(--ink-rgb),.55);
font-size:18px!important;line-height:1}
.hud-x:hover{color:rgba(var(--ink-rgb),.9)}}
@media screen and (max-width:719px){
.hud-title{font-size:12px}.hud-stat{top:33px;font-size:11.5px}
.hud-pop{top:58px;max-height:calc(100vh - 120px)}.hud-inst{font-size:9.5px;letter-spacing:.1em}
.hud-inst .k{margin-right:6px}.hud-tab{display:none}
P{top:auto;left:0;right:0;bottom:0;width:auto;height:38vh;height:38dvh;border-left:0;
border-top:1px solid rgba(var(--ink-rgb),.1);border-radius:14px 14px 0 0;padding:0 16px 24px;
transform:translateY(calc(100% - 46px));visibility:visible;
transition:transform .35s cubic-bezier(.2,.8,.2,1),height .3s cubic-bezier(.2,.8,.2,1)}
html.hud-on.hud-panel.hud-open:not(.hud-text) #app>main{transform:none}
html.hud-on.hud-panel.hud-open.hud-full:not(.hud-text) #app>main{height:88vh;height:88dvh}
html.hud-on:not(.hud-open):not(.hud-text) #app>main>:not(.hud-ph){visibility:hidden}
P>.hud-ph{margin:0 -16px 4px;padding:14px 12px 10px 16px;border-radius:14px 14px 0 0;touch-action:none}
P>.hud-ph .t{cursor:pointer;min-height:24px}
P>.hud-ph::before{content:"";position:absolute;left:50%;top:5px;width:34px;height:3px;margin-left:-17px;
border-radius:2px;background:rgba(var(--ink-rgb),.22)}
html.hud-on:not(.hud-open):not(.hud-text) #app>main>.hud-ph .hud-x{display:none}
html.hud-on.hud-full:not(.hud-text) .hud>.hud-tv,html.hud-on.hud-full:not(.hud-text) .hud>.hud-reset{visibility:hidden;
opacity:0;pointer-events:none}}
@media (prefers-reduced-motion:reduce){.hud *,html.hud-on #app>main{transition:none!important;animation:none!important}}
@media print{.hud,.sand,.sandtip{display:none!important}
html,body{height:auto!important;overflow:visible!important;background:#fff!important}
html.hud-on #app>main,#app>main{position:static!important;transform:none!important;visibility:visible!important;
display:block!important;width:auto!important;height:auto!important;max-width:none!important;overflow:visible!important;
background:none!important;border:0!important;LIGHT;color:#000!important}
#app>main>header,#app section.ready,#app section.progress,#app .tablewrap,#app section.results>h2{display:block!important}
#app .hud-ph,#app details.hud-lg{display:none!important}#app .legend{display:flex!important}}
""").replace("DARK", _DARK).replace("LIGHT", _LIGHT).replace("SANS", _SANS).replace("MONO", _MONO).replace("P{", P + "{") \
    .replace("\nP ", "\n" + P + " ").replace(",P ", "," + P + " ").replace("\nP>", "\n" + P + ">").replace(",P>", "," + P + ">") \
    .replace("}P ", "}" + P + " ")

# The HUD's script: runs after the page's own script (which rendered #app and exposed its data as window.__jevD) and
# before the scene's (page_sand.JS), which asks it for the panel's inset (window.__jevHud). It turns the HUD on only
# when the scene can run; if the scene then does not start, the plain page comes back (and the scene's box is hidden).
# While the checklist is open, or the pointer is on the results panel, the page's refresh waits (window.__jevSandBusy,
# at most 15 s after the last pointer or key event there); an open checklist comes back after a reload. The text
# view follows `#text` in the address both ways. On a phone the results are a bottom sheet: its head (a button)
# opens it to a peek (~38 % of the height, the ranked rows), then to the full page; dragging the head does the same.
JS = r"""
(function(){try{
var W=window,D=document,hud=D.getElementById('hud'),box=D.getElementById('sand'),app=D.getElementById('app');
if(!hud||!box||!app||!W.requestAnimationFrame||!D.documentElement)return;
var cv=box.querySelector('canvas');if(!cv||!cv.getContext)return;
var DE=D.documentElement,main=app.querySelector('main');if(!main)return;
var DATA=W.__jevD||{},ST=DATA.live||null;
var KEY='jevscreen-hud:'+(hud.getAttribute('data-key')||''),M={};
try{M=JSON.parse(W.sessionStorage.getItem(KEY)||'{}')||{};}catch(e){M={};}
function save(){try{M.sy=Math.round(main.scrollTop||0);W.sessionStorage.setItem(KEY,JSON.stringify(M));}catch(e){}}
var RED=false;try{RED=!!W.matchMedia('(prefers-reduced-motion: reduce)').matches;}catch(e){}
function phone(){try{return W.matchMedia('(max-width: 719px)').matches;}catch(e){return (W.innerWidth||1000)<720;}}
function has(el,c){return (' '+el.className+' ').indexOf(' '+c+' ')>=0;}
function setc(el,c,on){var h=has(el,c);if(on&&!h)el.className=(el.className?el.className+' ':'')+c;
 else if(!on&&h)el.className=(' '+el.className+' ').replace(' '+c+' ',' ').replace(/^\s+|\s+$/g,'');}
function $(id){return D.getElementById(id);}
function hashTV(){try{return String(W.location.hash||'')==='#text';}catch(e){return false;}}
var HAS=hud.getAttribute('data-panel')==='1',TV=hashTV()||M.tv===1;if(TV)M.tv=1;
var OPEN=HAS&&(typeof M.po==='number'?M.po===1:hud.getAttribute('data-open')==='1'),FULL=false,HELD=false;
var ph=$('hud-ph'),tab=$('hud-tab'),pt=$('hud-pt');if(ph&&HAS)main.insertBefore(ph,main.firstChild);
// the legend behind a small disclosure in the panel (the text view shows it as it is)
try{var lg=main.querySelector('section.results>.legend')||main.querySelector('.legend');if(lg&&lg.parentNode){var dl=D.createElement('details');dl.className='hud-lg';
 var sm=D.createElement('summary');sm.textContent=hud.getAttribute('data-w-legend')||'';dl.appendChild(sm);lg.parentNode.insertBefore(dl,lg);}}catch(e){}
// the scene's inset: the panel's width (desktop), the sheet's peek or its head (phone)
// the title and the status line end before the instrument corner (its measured width: --hud-iw)
var inst=hud.querySelector('.hud-inst'),IW=-1;
function instW(){if(!inst)return;var w=Math.ceil(inst.offsetWidth||0);if(w&&w!==IW){IW=w;try{DE.style.setProperty('--hud-iw',w+'px');}catch(e){}}}
function vars(){instW();var r=0,b=0;if(HAS&&!TV){if(phone())b=OPEN?Math.min(main.offsetHeight||0,Math.round((W.innerHeight||800)*0.4)):(ph?ph.offsetHeight:46);else r=OPEN?main.offsetWidth:0;}
 try{DE.style.setProperty('--hud-pr',r+'px');DE.style.setProperty('--hud-pb',b+'px');}catch(e){}return {r:r,b:b};}
function apply(){setc(DE,'hud-on',true);setc(DE,'hud-panel',HAS);setc(DE,'hud-open',OPEN);setc(DE,'hud-full',OPEN&&FULL);setc(DE,'hud-text',TV);
 if(tab)tab.hidden=!(HAS&&!OPEN&&!TV);var tv=$('hud-tv');if(tv)tv.setAttribute('aria-pressed',TV?'true':'false');
 if(pt){var ph_=phone();pt.setAttribute('aria-expanded',OPEN?'true':'false');pt.setAttribute('tabindex',ph_?'0':'-1');}vars();}
function relayout(){vars();if(W.__jevSand&&W.__jevSand.relayout)W.__jevSand.relayout();}
function setOpen(o,keep){OPEN=!!o&&HAS;if(!OPEN)FULL=false;if(keep){M.po=OPEN?1:0;save();}apply();relayout();}
function setFull(f){FULL=!!f&&OPEN;apply();}
// the text view: its button, the back button, and `#text` in the address (kept in step both ways)
function setHash(){try{if(W.history&&W.history.replaceState&&hashTV()!==TV){var l=W.location;W.history.replaceState(null,'',String(l.pathname||'')+String(l.search||'')+(TV?'#text':''));}}catch(e){}}
function setTV(on){TV=!!on;M.tv=TV?1:0;save();setHash();popShow(false);apply();if(W.__jevSand&&W.__jevSand.pause)W.__jevSand.pause(TV);relayout();
 try{var f=$(TV?'hud-back':'hud-tv');if(f&&f.focus)f.focus();}catch(e){}if(TV){try{W.scrollTo(0,0);}catch(e){}}}
apply();
// the status dot: the checklist (the page's own, cloned) on hover, a tap or the keys; a stopped worker turns it red
var stat=$('hud-stat'),pop=$('hud-pop'),PIN=false,HT=0,LASTP=0;
if(ST&&ST.stale&&ST.phase==='failed'&&stat){stat.className='hud-stat s-bad';var sy=D.createElement('span');sy.className='say';
 sy.textContent=String(ST.stale.text||'');var kids=stat.querySelectorAll('.say,.fix');for(var q=0;q<kids.length;q++)stat.removeChild(kids[q]);
 stat.appendChild(sy);stat.setAttribute('aria-label',String(ST.stale.text||''));var sg=$('hud-stage');if(sg)sg.textContent=hud.getAttribute('data-w-fail')||'';var dg=$('hud-doing');if(dg)dg.hidden=true;}
function fillPop(){if(!pop||pop.firstChild)return;var parts=[],qa=main.querySelectorAll('section.progress .alert'),qr=main.querySelectorAll('section.ready');
 for(var i0=0;i0<qa.length;i0++)parts.push(qa[i0]);for(var i1=0;i1<qr.length;i1++)parts.push(qr[i1]);   // a block first, then the checklist
 for(var i=0;i<parts.length;i++){var c=parts[i].cloneNode(true);var ids=c.querySelectorAll?c.querySelectorAll('[id]'):[];
  for(var j=0;j<ids.length;j++)ids[j].removeAttribute('id');if(c.removeAttribute)c.removeAttribute('id');
  var ok=c.querySelectorAll?c.querySelectorAll('details.okline'):[];for(var k=0;k<ok.length;k++)ok[k].open=true;pop.appendChild(c);}}
function popShow(on){if(!pop||!stat)return;if(on){fillPop();LASTP=Date.now();var r=stat.getBoundingClientRect();pop.style.top=Math.round(r.bottom+6)+'px';}
 pop.hidden=!on;stat.setAttribute('aria-expanded',on?'true':'false');if(!on)PIN=false;}
function touchP(){LASTP=Date.now();}
if(stat&&pop){
 stat.addEventListener('pointerenter',function(e){touchP();if(e.pointerType==='mouse'){clearTimeout(HT);popShow(true);}});
 var later=function(e){if(e.pointerType==='mouse'&&!PIN){clearTimeout(HT);HT=setTimeout(function(){popShow(false);},280);}};
 stat.addEventListener('pointerleave',later);pop.addEventListener('pointerleave',later);
 pop.addEventListener('pointerenter',function(){clearTimeout(HT);touchP();});
 stat.addEventListener('pointermove',touchP);pop.addEventListener('pointermove',touchP);pop.addEventListener('scroll',touchP,{passive:true});
 stat.addEventListener('click',function(){var on=pop.hidden||!PIN;popShow(on);PIN=on;});
 stat.addEventListener('keydown',function(e){touchP();if(e.key==='Enter'||e.key===' '){try{e.preventDefault();}catch(e2){}var on=pop.hidden;popShow(on);PIN=on;}});
 D.addEventListener('keydown',function(e){if(!pop.hidden)touchP();if(e.key==='Escape'&&!pop.hidden){popShow(false);try{stat.focus();}catch(e2){}}});
 D.addEventListener('pointerdown',function(e){if(pop.hidden)return;var n=e.target;while(n){if(n===pop||n===stat)return;n=n.parentNode;}popShow(false);},true);
 // an open checklist comes back after the page's reload (pinned: it closes on a click elsewhere or Esc)
 if(M.pop===1&&!TV){popShow(true);PIN=true;}}
// the time: ticking from the worker's start while it runs
var since=Date.parse(hud.getAttribute('data-since')||''),tel=$('hud-time');
function mmss(s){s=Math.max(0,Math.floor(s));var h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;
 return (h?h+':'+(m<10?'0':''):(m<10?'0':''))+m+':'+(x<10?'0':'')+x;}
if(!isNaN(since)&&tel){var stop=null;if(ST&&ST.stale&&ST.phase==='failed'){var hb=Date.parse(ST.stale.since);if(!isNaN(hb))stop=hb;}
 var tick=function(){tel.textContent=mmss(((stop||Date.now())-since)/1000);instW();};tick();if(!stop)setInterval(tick,1000);}
// the buttons
function on(id,f){var el=$(id);if(el)el.addEventListener('click',f);}
on('hud-tv',function(){setTV(true);});on('hud-back',function(){setTV(false);});
on('hud-reset',function(){if(W.__jevSand&&W.__jevSand.reset)W.__jevSand.reset();});
on('hud-tab',function(){setOpen(true,true);});
on('hud-hide',function(e){try{e.stopPropagation();}catch(e2){}setOpen(false,true);});
try{W.addEventListener('hashchange',function(){var t=hashTV();if(t!==TV)setTV(t);});}catch(e){}
// the phone's sheet: a tap on its head goes closed -> peek -> full -> peek; a drag up or down steps it
var DRG=null,SWALLOW=0;
function sheetTap(){if(!phone())return;if(!OPEN)setOpen(true,true);else setFull(!FULL);}
if(ph){ph.addEventListener('click',function(e){var n=e&&e.target;for(var i=0;n&&i<3;i++,n=n.parentNode)if(n.id==='hud-hide')return;if(Date.now()-SWALLOW<400)return;sheetTap();});
 ph.addEventListener('pointerdown',function(e){if(phone())DRG={y:e.clientY,id:e.pointerId};});
 ph.addEventListener('pointerup',function(e){if(!DRG)return;var dy=(e.clientY||0)-DRG.y;DRG=null;if(Math.abs(dy)<24)return;SWALLOW=Date.now();
  if(dy<0){if(!OPEN)setOpen(true,true);else setFull(true);}else{if(FULL)setFull(false);else setOpen(false,true);}});
 ph.addEventListener('pointercancel',function(){DRG=null;});}
// rows and grains light each other: a row pointed at (or focused) in the panel lights its grain
function rankOf(n){for(var k=0;n&&n!==main&&k<14;k++,n=n.parentNode){if(n.getAttribute){var r=n.getAttribute('data-rank');if(r)return Number(r);
 if(n.tagName&&String(n.tagName).toLowerCase()==='details'&&/^r\d+$/.test(n.id||''))return Number(n.id.slice(1));}}return null;}
var LR=null;function lightRow(r){if(r===LR)return;LR=r;if(W.__jevSand&&W.__jevSand.light)W.__jevSand.light(r);}
var PIN_IN=false,PANT=0;function touchPanel(){PANT=Date.now();}
main.addEventListener('pointerover',function(e){if(!TV)lightRow(rankOf(e.target));});
main.addEventListener('pointerenter',function(){PIN_IN=true;touchPanel();});
main.addEventListener('pointermove',touchPanel);main.addEventListener('pointerdown',touchPanel);
main.addEventListener('pointerleave',function(){PIN_IN=false;lightRow(null);});
main.addEventListener('focusin',function(e){if(!TV)lightRow(rankOf(e.target));});
var SVT=0;main.addEventListener('scroll',function(){if(TV)return;touchPanel();W.__jevSandBusy=Date.now();clearTimeout(SVT);SVT=setTimeout(save,300);},{passive:true});
if(HAS&&typeof M.sy==='number'&&!TV){try{main.scrollTop=M.sy;}catch(e){}}
// the page's refresh waits while the checklist is open or the pointer rests on the panel (15 s after the last event)
setInterval(function(){if(TV)return;var n=Date.now();if((pop&&!pop.hidden&&n-LASTP<15000)||((PIN_IN||phone())&&OOPEN()&&n-PANT<15000))W.__jevSandBusy=n;},500);
function OOPEN(){return HAS&&OPEN;}
// the reload keeps an open checklist
try{var prevSave=W.__jevSave;W.__jevSave=function(){try{if(prevSave)prevSave();}catch(e){}M.pop=(pop&&!pop.hidden&&!TV)?1:0;save();};
 W.addEventListener('pagehide',function(){M.pop=(pop&&!pop.hidden&&!TV)?1:0;save();});}catch(e){}
var HR=[];function hover(rank){for(var i=0;i<HR.length;i++)setc(HR[i],'sandhov',false);HR=[];if(rank===null||rank===undefined||TV)return;
 var d=$('r'+rank);if(d)HR.push(d);var trs=main.querySelectorAll('tr[data-rank="'+Number(rank)+'"]');for(var j=0;j<trs.length;j++)HR.push(trs[j]);
 for(var k=0;k<HR.length;k++)setc(HR[k],'sandhov',true);}
function pick(rank){if(TV||!HAS)return false;var d=$('r'+rank);if(!d)return false;if(!OPEN)setOpen(true,true);
 try{if(!d.open)d.open=true;}catch(e){}var top=Math.max(0,d.offsetTop-(ph?ph.offsetHeight:0)-12);
 try{main.scrollTo({top:top,behavior:RED?'auto':'smooth'});}catch(e){main.scrollTop=top;}
 setc(d,'sandlit',true);setTimeout(function(){setc(d,'sandlit',false);},1600);return true;}
W.__jevHud={inset:vars,pick:pick,hover:hover,
 hold:function(){if(HAS&&OPEN&&typeof M.po!=='number'){HELD=true;OPEN=false;apply();}},
 finished:function(){if(HAS&&!OPEN&&(HELD||typeof M.po!=='number')){HELD=false;setOpen(true,false);}}};
W.addEventListener('resize',function(){apply();});
// no scene after all (a browser without canvas 2D, a throw): the plain page comes back, the scene's box hidden
setTimeout(function(){if(!W.__jevSand){TV=true;apply();setc(DE,'hud-on',false);setc(DE,'hud-text',false);try{box.className='sand';}catch(e){}}},0);
}catch(e){try{var de=document.documentElement;de.className=String(de.className).replace(/\bhud-(on|panel|open|full|text)\b/g,'');
 var sb=document.getElementById('sand');if(sb)sb.className='sand';}catch(e2){}}})();
"""
