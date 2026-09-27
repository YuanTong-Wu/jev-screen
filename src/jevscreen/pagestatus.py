"""The status part ("live" in the page data) of the ONE page per idea (<home>/pages/<idea_key>.html): prerequisites, live progress and the
slot for scope questions, above the results (jevscreen.page renders it).

Rules:
- No network, never waits on the DuckDB store: everything comes from the quickstart job file (the worker's steps,
  progress, failures, approval), files on disk (consent answers, key presence by stat only, cooldown markers) and
  doctor's local checks (Python, installed packages). A key value is never read or shown.
- ONE language: every text is built here in the page's language (zh fully Chinese, en fully English); the page only
  shows it.
- Deterministic for the same inputs: no clock is read except to tell whether a cooldown marker is still active and
  how long a running stage has taken (ETA); the job's own timestamps are shown.
- write(cfg, job) rewrites the stable page atomically (tmp + os.replace) keeping the results the page already holds
  and replacing only its status; the worker calls it at every progress tick, the front at every call. The same lock
  as page.write_page's stable copy (page_lock) keeps the two from overwriting each other's results.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import functools
import os
import time
from pathlib import Path
from typing import Any, Callable, Iterator

STATES = ("ok", "run", "wait", "need", "fail", "opt")     # green check / spinner / grey / red cross / red / optional
FD_MB = 15.0                                              # the company-profile file, about 15 MB
UNIVERSE_S = 60                                           # a stock-list download takes about a minute
PAGE_EVERY_S = 2.0                                        # the worker rewrites the page at most this often per tick

T: dict[str, dict[str, str]] = {
    "zh": {
        "py_ok": "Python {v} 和所需组件已装好",
        "py_old": "Python {v} 太旧，需要 3.10 或更新的版本", "py_old_fix": "最简单：安装 uv（一条命令），它自带合适的 Python",
        "duck_missing": "缺少数据库组件 duckdb", "duck_fix": "让你的 AI 按安装步骤重新安装，然后重跑",
        "consent_ok": "你已同意使用两个数据源（TradingView 股票清单、Yahoo 公司简介），只供个人研究",
        "consent_need": "还没回答能不能用两个数据源（TradingView 股票清单、Yahoo 公司简介）",
        "consent_need_fix": "你的 AI 会问你一句，回答「可以」或「不要」就行",
        "consent_no": "你选择了不使用这两个数据源，筛选停在这里",
        "consent_no_fix": "改主意了？告诉你的 AI「我同意使用这些来源」",
        "key_none": "还没有 Jev 的 key（AI 读简介和年报要用，从你自己的账户付费）",
        "key_none_fix": "告诉你的 AI 你有哪一家的账号（推荐先试 TypeSafe 官方；官方暂停注册时用 OpenRouter），"
                        "然后在弹出的输入框里粘贴 key。千万别把 key 发到聊天里",
        "key_chosen_missing": "已选 {p}，但还没设置它的 key",
        "key_chosen_fix": "在 {p} 创建 key，再让你的 AI 弹出输入框设置",
        "key_rejected": "{p} 不接受这个 key", "key_rejected_fix": "重新创建一个 key，再让你的 AI 弹出输入框设置",
        "key_vercel_403": "{p} 不接受这个 key 用 Jev：多半是还没买 AI Gateway credits",
        "key_vercel_403_fix": "在 {url} 买一点 credits，或重新创建 key 再设置",
        "key_no_credit": "{p} 说账户没有余额", "key_no_credit_fix": "去 {url} 充值，然后告诉你的 AI「好了」",
        "key_ok": "Jev key 已设置，并已验证可以付费（{p}）",
        "key_ok_used": "Jev key 可以用（{p}），这次筛选就是用它付的费",
        "key_checking": "正在用约 $0.0001 验证 key 能不能付费（{p}）",
        "key_set": "Jev key 已设置（{p}）；开始筛选前会先用约 $0.0001 验证能不能付费",
        "uni_ok": "股票清单：{n} 家公司（市值 ≥ {floor}）",
        "uni_fresh": "股票清单：{days} 天内下载过，不用重下",
        "uni_old": "股票清单：这次没更新，先用已有的清单",
        "uni_run": "正在下载股票清单（TradingView，免费，约 1 分钟）",
        "uni_wait": "股票清单：还没开始（同意使用数据源后自动下载，免费）",
        "uni_blocked": "股票清单：TradingView 暂时拒绝了请求，{at} 后才能再试",
        "net_failed": "股票清单：网络连不上，{at} 后再试",
        "desc_blocked": "公司简介：下载地址暂时拒绝了请求，{at} 后才能再试",
        "desc_failed": "公司简介：没下载下来（网络可能被限速或连不上）",
        "desc_failed_fix": "15 分钟后让你的 AI 重跑；或按下面红框里的办法用浏览器下载",
        "stop_fix": "不是你的操作问题；到时让你的 AI 重跑同一条命令，做完的步骤不会重做",
        "desc_ok": "公司简介：{pct}% 的公司有简介",
        "desc_ok_n": "公司简介：{n} 家有简介（{pct}%）",
        "desc_old": "公司简介：这次没下载成功，先用已有的简介",
        "desc_run": "正在下载公司简介（FinanceDatabase，约 15 MB，免费）",
        "desc_wait": "公司简介：还没开始（股票清单下载完就开始）",
        "pack_ok": "开放数据包：已导入", "pack_skip": "开放数据包：这台电脑没有配置，日本公司先用简介核对",
        "pack_run": "正在导入开放数据包", "pack_wait": "开放数据包：还没开始",
        "ready_ok": "可以开始筛选", "ready_done": "筛选做完了",
        "ready_screening": "正在筛选",
        "ready_budget": "等你同意花费上限：预计约 ${est}，最多 ${x}",
        "ready_budget_fix": "回答你的 AI 的问题（同意的话说「可以」）",
        "ready_over": "预计要预留 ${r}，超过你同意的 ${x}", "ready_over_fix": "回答你的 AI：缩小范围，或同意一个更高的上限",
        "ready_topup": "预算用完了：继续需要再同意约 ${more}", "ready_topup_fix": "回答你的 AI 的问题",
        "ready_uncertain": "上次中断时有 {n} 条请求结果不明，重发约 ${c}", "ready_uncertain_fix": "回答你的 AI 的问题",
        "ready_idea_en": "你的 AI 正在把你的想法写成一句英文（发给 Jev 的）",
        "ready_wait": "上面几项都好了就开始",
        "opt_sec_ok": "美国 SEC 联系名字和邮箱：已设置（美股用年报原文核对）",
        "opt_sec": "可选：美国 SEC 联系名字和邮箱（美股改用年报原文核对，不用注册账号）",
        "opt_sec_no": "美国 SEC 联系方式：你选择了不提供（美股只用简介核对）",
        "opt_edinet_ok": "日本 EDINET 的 key：已设置", "opt_edinet": "可选：日本 EDINET 的 key（日本年报原文）",
        "opt_dart_ok": "韩国 OpenDART 的 key：已设置", "opt_dart": "可选：韩国 OpenDART 的 key（韩国年报原文）",
        "opt_mops_ok": "台湾年报下载（公开资讯观测站）：已开启",
        "opt_mops": "可选：台湾年报下载（公开资讯观测站，前 10 名里有台湾公司时才会问）",
        "all_ok": "准备就绪：安装、数据授权、Jev key（{p}，已验证）、股票清单和公司简介都好了",
        "all_ok_nokey": "准备就绪：安装、数据授权、股票清单和公司简介都好了",
        "ph_fresh": "还没开始", "ph_check": "正在检查安装", "ph_download": "正在下载数据（免费）",
        "ph_prepare": "正在准备", "ph_screen": "AI 正在筛选", "ph_fetch": "正在补年报原文",
        "ph_finish": "正在生成结果", "ph_wait_you": "等你回答（看下面的红叉）", "ph_wait_ai": "等你的 AI",
        "ph_done": "完成", "ph_partial": "部分完成", "ph_budget": "预算用完（部分结果）",
        "ph_blocked": "暂停：网站暂时拒绝了请求", "ph_failed": "出错停下了", "ph_key": "等你处理 key",
        "ph_declined": "已停止", "ph_fill": "正在补公司简介并重新排序", "ph_stopped": "后台任务中断了",
        "ph_busy": "排队中：另一个任务正在用数据库", "ph_review": "你的 AI 正在逐家核对摘录",
        "review_banner": "你的 AI 正在逐家核对摘录，完成后这页会更新（约 2–4 分钟）",
        "review_over": "你的 AI 没有完成核对，先看系统的名单。",
        "it_universe": "股票清单（TradingView）", "it_desc": "公司简介（FinanceDatabase）", "it_pack": "开放数据包",
        "it_l1": "AI 初读公司简介", "it_l2": "AI 用年报/简介核对", "it_fetch": "补抓年报原文",
        "it_fill": "补公司简介（TradingView）",
        "n_companies": "{n} 家公司", "n_desc": "{n} 家有简介", "n_of": "{done} / {total}", "n_filled": "补到 {n} 家",
        "it_fill_default": "补缺简介的公司（TradingView，免费）",
        "mb_of": "{done} / {total} MB", "mb_run": "下载中…", "running": "进行中…", "waiting": "等待开始",
        "skipped": "跳过", "done": "完成", "stopped": "停下了（见上面红框）",
        "eta": "还要约 {m} 分钟", "eta_lt1": "还要不到 1 分钟",
        "spent": "已花 ${x}（{cny}），你同意的上限 ${cap}", "spent_nocap": "已花 ${x}（{cny}）",
        "cny": "约 ¥{y}", "cny_tiny": "不到 ¥0.01",
        "cool": "{src}在 {at} 拒绝过我们的请求：为了守规矩，24 小时内不再请求它，{until} 后自动恢复。不是你的操作问题。",
        "updated": "更新于 {at}",
    },
    "en": {
        "py_ok": "Python {v} and the needed packages are installed",
        "py_old": "Python {v} is too old; 3.10 or newer is needed",
        "py_old_fix": "Easiest fix: install uv (one command); it brings its own Python",
        "duck_missing": "The database component duckdb is missing", "duck_fix": "Have your AI reinstall, then rerun",
        "consent_ok": "You agreed to the two data sources (TradingView's stock list, Yahoo profiles), for your own "
                      "research only",
        "consent_need": "Not answered yet: may the two data sources be used (TradingView's stock list, Yahoo "
                        "profiles)?",
        "consent_need_fix": "Your AI will ask you; answer yes or no",
        "consent_no": "You chose not to use these two data sources, so screening stops here",
        "consent_no_fix": "Changed your mind? Tell your AI 'I agree to use these sources'",
        "key_none": "No Jev key yet (the AI that reads profiles and reports; paid from your own account)",
        "key_none_fix": "Tell your AI which account you have (try TypeSafe's official API first; OpenRouter when its "
                        "sign-ups are paused), then paste the key into the box that opens. Never paste it into the chat",
        "key_chosen_missing": "{p} was chosen, but its key is not set yet",
        "key_chosen_fix": "Create a key at {p}, then have your AI open the key box",
        "key_rejected": "{p} rejected the key", "key_rejected_fix": "Create a new key and have your AI open the key box",
        "key_vercel_403": "{p} refused this key for Jev: most likely no AI Gateway credits were bought yet",
        "key_vercel_403_fix": "Buy some credits at {url}, or create a new key and set it again",
        "key_no_credit": "{p} says the account has no credit", "key_no_credit_fix": "Top up at {url}, then tell your AI "
                                                                                    "'done'",
        "key_ok": "Jev key set and checked: it can pay ({p})",
        "key_ok_used": "Jev key works ({p}); this screen was paid with it",
        "key_checking": "Checking with a tiny paid request (about $0.0001) that the key can pay ({p})",
        "key_set": "Jev key set ({p}); a tiny paid request (about $0.0001) checks it before the screen",
        "uni_ok": "Stock list: {n} companies (market cap ≥ {floor})",
        "uni_fresh": "Stock list: downloaded within {days} days; no new download needed",
        "uni_old": "Stock list: not refreshed this time; using the one already here",
        "uni_run": "Downloading the stock list (TradingView, free, about 1 min)",
        "uni_wait": "Stock list: not started (downloads by itself once the data sources are agreed; free)",
        "uni_blocked": "Stock list: TradingView refused our requests for now; retry after {at}",
        "net_failed": "Stock list: no network; retry after {at}",
        "desc_blocked": "Company profiles: the download hosts refused our requests for now; retry after {at}",
        "desc_failed": "Company profiles: the download did not come through (the network may be slow or blocked)",
        "desc_failed_fix": "Have your AI rerun in 15 minutes, or download the file in a browser as the red box below "
                           "says",
        "stop_fix": "Not something you did; then have your AI rerun the same command (finished steps are not redone)",
        "desc_ok": "Company profiles: {pct}% of companies have one",
        "desc_ok_n": "Company profiles: {n} companies have one ({pct}%)",
        "desc_old": "Company profiles: the download failed this time; using the ones already here",
        "desc_run": "Downloading company profiles (FinanceDatabase, about 15 MB, free)",
        "desc_wait": "Company profiles: not started (right after the stock list)",
        "pack_ok": "Open data pack: imported",
        "pack_skip": "Open data pack: not configured on this computer; Japanese companies are checked from profiles",
        "pack_run": "Importing the open data pack", "pack_wait": "Open data pack: not started",
        "ready_ok": "Ready to screen", "ready_done": "The screen is done", "ready_screening": "Screening",
        "ready_budget": "Waiting for your OK on the spending cap: about ${est} expected, at most ${x}",
        "ready_budget_fix": "Answer your AI's question (say yes if it is OK)",
        "ready_over": "This needs a reservation of ${r}, above the ${x} you approved",
        "ready_over_fix": "Answer your AI: narrow the scope, or approve a higher cap",
        "ready_topup": "The budget ran out: continuing needs about ${more} more", "ready_topup_fix": "Answer your AI's "
                                                                                                    "question",
        "ready_uncertain": "{n} requests had an unknown outcome when the run stopped; resending costs about ${c}",
        "ready_uncertain_fix": "Answer your AI's question",
        "ready_idea_en": "Your AI is writing your idea as one English sentence (what Jev reads)",
        "ready_wait": "Starts as soon as everything above is ready",
        "opt_sec_ok": "US SEC contact name and e-mail: set (US companies are checked against annual reports)",
        "opt_sec": "Optional: a US SEC contact name and e-mail (check US companies against annual reports; no account)",
        "opt_sec_no": "US SEC contact: you chose not to give one (US companies are checked from profiles only)",
        "opt_edinet_ok": "Japan EDINET key: set", "opt_edinet": "Optional: a Japan EDINET key (Japanese annual reports)",
        "opt_dart_ok": "Korea OpenDART key: set", "opt_dart": "Optional: a Korea OpenDART key (Korean annual reports)",
        "opt_mops_ok": "Taiwan annual reports (MOPS): on",
        "opt_mops": "Optional: Taiwan annual reports (MOPS; asked only when a Taiwan company is in the top 10)",
        "all_ok": "Ready: install, data sources, Jev key ({p}, checked), stock list and company profiles are all set",
        "all_ok_nokey": "Ready: install, data sources, stock list and company profiles are all set",
        "ph_fresh": "Not started", "ph_check": "Checking the install", "ph_download": "Downloading data (free)",
        "ph_prepare": "Preparing", "ph_screen": "The AI is screening", "ph_fetch": "Fetching annual reports",
        "ph_finish": "Building the results", "ph_wait_you": "Waiting for you (see the red crosses below)",
        "ph_wait_ai": "Waiting for your AI", "ph_done": "Done", "ph_partial": "Partly done",
        "ph_budget": "Budget ran out (partial)", "ph_blocked": "Paused: a site refused our requests for now",
        "ph_failed": "Stopped with an error", "ph_key": "Waiting for you to fix the key", "ph_declined": "Stopped",
        "ph_fill": "Filling company profiles and re-ranking", "ph_stopped": "The background work stopped",
        "ph_busy": "Queued: another task is using the database", "ph_review": "Your AI is checking the excerpts",
        "review_banner": "Your AI is checking the excerpts company by company; this page updates when it is done "
                         "(about 2-4 minutes)",
        "review_over": "Your AI did not finish checking the excerpts; here is the system's list for now.",
        "it_universe": "Stock list (TradingView)", "it_desc": "Company profiles (FinanceDatabase)",
        "it_pack": "Open data pack", "it_l1": "AI first read of the profiles", "it_l2": "AI check against reports / "
                                                                                        "profiles",
        "it_fetch": "Fetching annual reports", "it_fill": "Filling company profiles (TradingView)",
        "n_companies": "{n} companies", "n_desc": "{n} with a profile", "n_of": "{done} / {total}",
        "n_filled": "{n} filled", "it_fill_default": "Filling missing profiles (TradingView, free)",
        "mb_of": "{done} / {total} MB", "mb_run": "downloading…", "running": "running…", "waiting": "waiting",
        "skipped": "skipped", "done": "done", "stopped": "stopped (see the red box above)",
        "eta": "about {m} min left", "eta_lt1": "under a minute left",
        "spent": "Spent ${x} of the ${cap} you approved", "spent_nocap": "Spent ${x}",
        "cny": "", "cny_tiny": "",
        "cool": "{src} refused our requests at {at}. To stay polite we will not ask it again for 24 hours; it resumes "
                "by itself after {until}. Not something you did.",
        "updated": "updated {at}",
    },
}

# the command of a cooldown marker -> the site in plain words (zh, en)
COOL_SOURCES = {"refresh-universe": ("TradingView 股票清单", "TradingView's stock list"),
                "crawl-descriptions": ("TradingView 公司简介页", "TradingView's company pages"),
                "fetch-fd": ("公司简介的下载地址", "The company-profile download hosts"),
                "sync-sec": ("美国 SEC", "The US SEC"), "sync-cninfo": ("巨潮资讯网", "CNINFO"),
                "sync-edinet": ("日本 EDINET", "Japan's EDINET"), "sync-dart": ("韩国 OpenDART", "Korea's OpenDART"),
                "sync-bse": ("印度孟买证券交易所", "India's BSE"), "sync-mops": ("台湾公开资讯观测站", "Taiwan's MOPS")}
COOL_ORDER = tuple(COOL_SOURCES)


# ------------------------------------------------------------------------------------------------ small helpers

def _lang(lang: str | None) -> str:
    return "en" if lang == "en" else "zh"


def _parse(s: Any) -> dt.datetime | None:
    """An ISO time of the job file (UTC, 'Z' or naive) as naive UTC."""
    if isinstance(s, dt.datetime):
        return s.replace(tzinfo=None)
    if not isinstance(s, str) or not s:
        return None
    try:
        t = dt.datetime.fromisoformat(s.rstrip("Z"))
    except ValueError:
        return None
    return t.astimezone(dt.timezone.utc).replace(tzinfo=None) if t.tzinfo else t


def when_words(value: Any, lang: str) -> str:
    """A UTC time as the reader's local clock: '9月28日 14:05' / 'Sep 28, 14:05'."""
    t = _parse(value)
    if t is None:
        return "?"
    local = t.replace(tzinfo=dt.timezone.utc).astimezone()
    if lang == "zh":
        return f"{local.month}月{local.day}日 {local:%H:%M}"
    return f"{local:%b} {local.day}, {local:%H:%M}"


def eta_words(seconds: float | None, lang: str) -> str | None:
    if seconds is None or seconds < 0:
        return None
    S = T[lang]
    return S["eta_lt1"] if seconds < 60 else S["eta"].format(m=int(round(seconds / 60)))


def money_words(usd: float, lang: str) -> tuple[str, str]:
    """('0.12', '约 ¥0.86') - the yuan figure only on a Chinese page."""
    x = f"{usd:.2f}" if usd >= 0.01 or usd == 0 else f"{usd:.4f}"
    if lang != "zh":
        return x, ""
    y = usd * 7.2
    return x, (T["zh"]["cny_tiny"] if 0 < y < 0.005 else T["zh"]["cny"].format(y=f"{y:.2f}"))


def _floor_words(v: Any, lang: str) -> str:
    from . import l10n
    return l10n.usd_words(float(v or 1e9), lang)


def _step(job: dict[str, Any] | None, sid: str) -> dict[str, Any] | None:
    return next((s for s in (job or {}).get("steps") or [] if s.get("id") == sid), None)


def _done(job: dict[str, Any] | None, sid: str) -> bool:
    s = _step(job, sid)
    return bool(s) and s.get("status") in ("ok", "skipped")


@functools.lru_cache(maxsize=1)
def _install() -> tuple[str, bool, bool]:
    """(python version, python ok, duckdb installed): doctor's local checks, once per process."""
    from . import doctor
    py = doctor.check_python()
    deps = doctor.check_dependencies()
    v = ".".join(str(x) for x in __import__("sys").version_info[:3])
    return v, py["status"] == "ok", deps[0]["status"] == "ok"


def _spent(job: dict[str, Any]) -> float | None:
    """What the approval has spent, from the job file only (the worker's last ledger reading, the costs of its runs,
    the key test); None without an approval. During a screen the running cost of the screen's layers is added."""
    a = job.get("approval")
    if not a:
        return None
    recorded = sum(float(x or 0.0) for x in (a.get("costs") or {}).values())
    base = float(a.get("canary_usd") or 0.0) + max(float(a.get("ledger_usd") or 0.0), recorded)
    p = job.get("progress") or {}
    if job.get("state") == "running" and p.get("run_spent_usd") is not None and p.get("spent_before") is not None:
        base = max(base, float(p["spent_before"]) + float(p["run_spent_usd"]))
    return round(base, 6)


def _check(cid: str, state: str, text: str, fix: str | None = None) -> dict[str, Any]:
    assert state in STATES
    return {"id": cid, "state": state, "text": text, "fix": fix}


# ------------------------------------------------------------------------------------------------ the checklist

def _key_check(cfg, job: dict[str, Any] | None, lang: str, result_ok: bool) -> tuple[dict[str, Any], str | None]:
    """(the Jev key line, the provider as shown) - configured AND verified by the canary, with the provider's name
    (Vercel: 'version cannot be pinned')."""
    from . import jev, quickstart
    S = T[lang]
    k = quickstart.key_state(cfg)
    try:
        pr = jev.provider_named(k.get("provider")) if k.get("provider") else None
    except jev.ProviderError:
        pr = None
    shown = jev.provider_title(pr, lang) if pr is not None else None
    if not k.get("configured"):
        if k.get("reason") in ("default", "error") or pr is None:
            return _check("key", "need", S["key_none"], S["key_none_fix"]), None
        return _check("key", "need", S["key_chosen_missing"].format(p=shown),
                      S["key_chosen_fix"].format(p=pr.key_url)), shown
    canary = (job or {}).get("canary") or {}
    same = canary.get("fingerprint") == k.get("fingerprint")
    st = canary.get("status")
    failure = (job or {}).get("failure") or {}
    fail_http = failure.get("http_status") if job and job.get("state") == "failed" and \
        failure.get("kind") == "ai_unavailable" else None
    if fail_http in (401, 402, 403) and (not canary or same) and st not in (401, 402, 403):
        st, same = fail_http, True          # the screen itself was refused (its canary was reset)
    if same and st == 402:
        return _check("key", "fail", S["key_no_credit"].format(p=shown),
                      S["key_no_credit_fix"].format(url=pr.credits_url)), shown
    if same and st in (401, 403):
        if pr.name == "vercel" and st == 403:
            return _check("key", "fail", S["key_vercel_403"].format(p=shown),
                          S["key_vercel_403_fix"].format(url=pr.credits_url)), shown
        return _check("key", "fail", S["key_rejected"].format(p=shown), S["key_rejected_fix"]), shown
    if same and st == "ok":
        return _check("key", "ok", S["key_ok"].format(p=shown)), shown
    if job and job.get("state") == "running" and job.get("current_step") == "ai_check":
        return _check("key", "run", S["key_checking"].format(p=shown)), shown
    if result_ok:
        return _check("key", "ok", S["key_ok_used"].format(p=shown)), shown
    return _check("key", "wait", S["key_set"].format(p=shown)), shown


def _running(job: dict[str, Any] | None, sid: str) -> bool:
    return bool(job) and job.get("state") == "running" and job.get("current_step") == sid


def _stopped_at(job: dict[str, Any] | None, sid: str) -> str | None:
    """The kind of the failure that stopped the job at step `sid` (blocked / network / fd), else None."""
    f = (job or {}).get("failure") or {}
    if (job or {}).get("state") == "failed" and f.get("kind") in ("blocked", "network", "fd") and \
            (job or {}).get("current_step") == sid:
        return f["kind"]
    return None


def _data_checks(job: dict[str, Any] | None, lang: str, result: dict[str, Any] | None) -> list[dict[str, Any]]:
    from . import quickstart
    S = T[lang]
    ra = when_words(((job or {}).get("failure") or {}).get("retry_after"), lang)
    floor = _floor_words((job or {}).get("min_mcap_usd") or ((result or {}).get("funnel") or {}).get("floor"), lang)
    out = []
    # stock list
    s = _step(job, "universe")
    if s and s.get("status") == "ok":
        n = s.get("companies")
        out.append(_check("universe", "ok", S["uni_ok"].format(n=f"{int(n):,}", floor=floor) if n is not None
                          else S["uni_fresh"].format(days=quickstart.UNIVERSE_FRESH_DAYS)))
    elif s and s.get("status") == "skipped":
        fresh = not s.get("blocked") and not s.get("stale")
        if s.get("companies") is not None and fresh:
            text = S["uni_ok"].format(n=f"{int(s['companies']):,}", floor=floor)
        else:
            text = S["uni_fresh"].format(days=quickstart.UNIVERSE_FRESH_DAYS) if fresh else S["uni_old"]
        out.append(_check("universe", "ok", text))
    elif _running(job, "universe"):
        out.append(_check("universe", "run", S["uni_run"]))
    elif _stopped_at(job, "universe"):
        k = _stopped_at(job, "universe")
        out.append(_check("universe", "fail", S["uni_blocked" if k == "blocked" else "net_failed"].format(at=ra),
                          S["stop_fix"]))
    elif result is not None and (result.get("funnel") or {}).get("universe"):
        out.append(_check("universe", "ok", S["uni_ok"].format(n=f"{int(result['funnel']['universe']):,}",
                                                               floor=floor)))
    else:
        out.append(_check("universe", "wait", S["uni_wait"]))
    # company profiles
    s = _step(job, "descriptions")
    if s and s.get("status") in ("ok", "skipped"):
        pct = round(100 * float(s.get("share") or 0.0))
        if s.get("stale"):
            out.append(_check("descriptions", "ok", S["desc_old"]))
        elif s.get("described") is not None:
            out.append(_check("descriptions", "ok", S["desc_ok_n"].format(n=f"{int(s['described']):,}", pct=pct)))
        else:
            out.append(_check("descriptions", "ok", S["desc_ok"].format(pct=pct)))
    elif _running(job, "descriptions"):
        out.append(_check("descriptions", "run", S["desc_run"]))
    elif _stopped_at(job, "descriptions"):
        k = _stopped_at(job, "descriptions")
        out.append(_check("descriptions", "fail", S["desc_blocked" if k == "blocked" else "desc_failed"].format(at=ra),
                          S["stop_fix"] if k == "blocked" else S["desc_failed_fix"]))
    elif result is not None and (result.get("funnel") or {}).get("described"):
        out.append(_check("descriptions", "ok", S["desc_ok_n"].format(
            n=f"{int(result['funnel']['described']):,}",
            pct=round(100 * result["funnel"]["described"] / max(1, result["funnel"].get("universe") or 1)))))
    else:
        out.append(_check("descriptions", "wait", S["desc_wait"]))
    # open data pack
    s = _step(job, "pack")
    if s and s.get("status") == "ok":
        out.append(_check("pack", "ok", S["pack_ok"]))
    elif s and s.get("status") == "skipped" or (job is None and result is not None):
        out.append(_check("pack", "ok", S["pack_skip"]))
    elif _running(job, "pack"):
        out.append(_check("pack", "run", S["pack_run"]))
    else:
        out.append(_check("pack", "wait", S["pack_wait"]))
    return out


def _ready_check(job: dict[str, Any] | None, lang: str, result: dict[str, Any] | None) -> dict[str, Any]:
    from . import quickstart
    S = T[lang]
    if job is None:
        return _check("ready", "ok" if result is not None else "wait",
                      S["ready_done"] if result is not None else S["ready_wait"])
    st = job.get("state")
    if st == "done" or _done(job, "screen"):
        return _check("ready", "ok", S["ready_done"])
    if st == "running" and job.get("current_step") in ("screen", "fetch", "finish"):
        return _check("ready", "run", S["ready_screening"])
    if job.get("waiting_on") == "approve_budget":
        kind = job.get("waiting_kind") or "first"
        a = job.get("approval") or {}
        est = job.get("estimate") or {}
        if kind == "over":
            return _check("ready", "need", S["ready_over"].format(r=_usd(est.get("est_reserved_usd")),
                                                                  x=f"{float(a.get('usd') or 0):g}"),
                          S["ready_over_fix"])
        if kind == "topup":
            return _check("ready", "need", S["ready_topup"].format(more=_usd(job.get("topup_usd") or 0.25)),
                          S["ready_topup_fix"])
        if kind == "uncertain":
            return _check("ready", "need", S["ready_uncertain"].format(n=int(job.get("uncertain_items") or 0),
                                                                       c=_usd(job.get("uncertain_usd"))),
                          S["ready_uncertain_fix"])
    if not job.get("idea_en"):
        return _check("ready", "wait", S["ready_idea_en"])
    if not quickstart.approval_valid(job):
        est = (job.get("estimate") or {}).get("est_cost_usd")
        e = _usd(est) if est is not None else f"{quickstart.EXPECTED_RANGE[0]}–{quickstart.EXPECTED_RANGE[1]}"
        return _check("ready", "need", S["ready_budget"].format(est=e, x=f"{quickstart.DEFAULT_APPROVAL_USD:g}"),
                      S["ready_budget_fix"])
    return _check("ready", "wait", S["ready_wait"])


def _usd(v: Any) -> str:
    if v is None:
        return "?"
    v = float(v)
    return f"{v:.2f}" if v >= 0.01 or v == 0 else f"{v:.4f}"


def _optional(cfg, lang: str) -> list[dict[str, Any]]:
    from . import consent, keys, ondemand
    S = T[lang]
    out = []
    has = {n: bool(keys.presence(cfg, n).get("configured")) for n in ("sec-email", "edinet", "opendart")}
    from . import quickstart
    sec_key = "opt_sec_ok" if has["sec-email"] else "opt_sec_no" if quickstart.sec_declined(cfg) else "opt_sec"
    out.append(_check("opt_sec", "ok" if has["sec-email"] else "opt", S[sec_key]))
    out.append(_check("opt_edinet", "ok" if has["edinet"] else "opt",
                      S["opt_edinet_ok" if has["edinet"] else "opt_edinet"]))
    out.append(_check("opt_opendart", "ok" if has["opendart"] else "opt",
                      S["opt_dart_ok" if has["opendart"] else "opt_dart"]))
    mops = consent.get(cfg, ondemand.MOPS_TOPIC)["state"] == "yes" if ondemand.MOPS_TOPIC in consent.TOPICS else False
    out.append(_check("opt_mops", "ok" if mops else "opt", S["opt_mops_ok" if mops else "opt_mops"]))
    return out


# ------------------------------------------------------------------------------------------------ progress

def _bar(iid: str, label: str, state: str, *, done: Any = None, total: Any = None, text: str | None = None,
         eta: str | None = None) -> dict[str, Any]:
    pct = None
    if state == "ok":
        pct = 100
    elif isinstance(done, (int, float)) and isinstance(total, (int, float)) and total > 0:
        pct = max(0, min(100, int(100 * float(done) / float(total))))
    return {"id": iid, "label": label, "state": state, "pct": pct, "text": text, "eta": eta}


def _stage_eta(st: dict[str, Any], now: dt.datetime) -> float | None:
    """Seconds left of a running stage from its own pace (done since it started, time since it started)."""
    t0, done, total = _parse(st.get("started_at")), st.get("done"), st.get("total")
    d0 = int(st.get("done0") or 0)
    if t0 is None or not isinstance(done, int) or not isinstance(total, int) or done <= d0 or total <= done:
        return None if not (isinstance(done, int) and isinstance(total, int) and total and done >= total) else 0.0
    rate = (done - d0) / max(1.0, (now - t0).total_seconds())
    return (total - done) / rate if rate > 0 else None


def _with_fill_file(cfg, job: dict[str, Any] | None) -> dict[str, Any] | None:
    """The job as the page shows it: a default fill the job still calls running while its child already wrote its
    ending (the worker settles it later, e.g. after the key) shows as ended, and the human's opt-out as skipped."""
    fdf = (job or {}).get("fill_default") or {}
    if fdf.get("state") != "running":
        return job
    from . import quickstart
    if job.get("fill_pref") == "no":
        return {**job, "fill_default": {**fdf, "state": "opted_out"}}
    st = quickstart.read_fill_state(cfg, job.get("idea_key") or "")
    if st.get("state") == "finished" and st.get("pid") == fdf.get("pid"):
        return {**job, "fill_default": {**fdf, "state": "done" if st.get("exit") == 0 else "failed", "added": None}}
    return job


def _progress(job: dict[str, Any] | None, lang: str, now: dt.datetime) -> dict[str, Any] | None:
    """Per data item done/total with bars, MB downloaded, ETA and $ spent; during screening the AI's reads."""
    from . import quickstart
    if not job:
        return None
    S = T[lang]
    p = job.get("progress") or {}
    stages = p.get("stages") if isinstance(p.get("stages"), dict) else {}
    items = []
    st = job.get("state")
    # stock list
    s = _step(job, "universe")
    if s and s.get("status") in ("ok", "skipped"):
        n = s.get("companies")
        items.append(_bar("universe", S["it_universe"], "ok",
                          text=S["n_companies"].format(n=f"{int(n):,}") if n is not None else S["done"]))
    elif _running(job, "universe"):
        t0 = _parse(p.get("stage_started_at")) if p.get("stage") == "universe" else None
        left = UNIVERSE_S - (now - t0).total_seconds() if t0 else UNIVERSE_S
        items.append(_bar("universe", S["it_universe"], "run", text=S["running"],
                          eta=eta_words(max(left, 10.0), lang)))
    elif _stopped_at(job, "universe"):
        items.append(_bar("universe", S["it_universe"], "fail", text=S["stopped"]))
    else:
        items.append(_bar("universe", S["it_universe"], "wait", text=S["waiting"]))
    # company profiles: MB downloaded while the file comes in
    s = _step(job, "descriptions")
    if s and s.get("status") in ("ok", "skipped"):
        pct = round(100 * float(s.get("share") or 0.0))
        mb = s.get("mb")
        text = (S["n_desc"].format(n=f"{int(s['described']):,}") if s.get("described") is not None else f"{pct}%")
        if mb:
            text += f" · {float(mb):.1f} MB"
        items.append(_bar("descriptions", S["it_desc"], "ok", text=text))
    elif _running(job, "descriptions"):
        b = stages.get("descriptions") or {}
        done_b, tot_b = b.get("done"), b.get("total") or FD_MB * 1e6
        if isinstance(done_b, (int, float)) and done_b > 0:
            text = S["mb_of"].format(done=f"{done_b / 1e6:.1f}", total=f"{tot_b / 1e6:.0f}")
            eta = eta_words(_stage_eta({"started_at": b.get("started_at"), "done": int(done_b),
                                        "total": int(tot_b), "done0": 0}, now), lang)
            items.append(_bar("descriptions", S["it_desc"], "run", done=done_b, total=tot_b, text=text, eta=eta))
        else:
            items.append(_bar("descriptions", S["it_desc"], "run", text=S["mb_run"]))
    elif _stopped_at(job, "descriptions"):
        items.append(_bar("descriptions", S["it_desc"], "fail", text=S["stopped"]))
    else:
        items.append(_bar("descriptions", S["it_desc"], "wait", text=S["waiting"]))
    # the default fill of the idea's market (free, in the background, before the first read)
    fdf = job.get("fill_default") or {}
    if fdf.get("state") == "running":
        done_f, tot_f = fdf.get("done"), fdf.get("total")
        items.append(_bar("fill_default", S["it_fill_default"], "run", done=done_f, total=tot_f,
                          text=S["n_of"].format(done=f"{int(done_f or 0):,}", total=f"{int(tot_f or 0):,}")
                          if tot_f and done_f else S["running"]))
    elif fdf.get("state") == "done":
        items.append(_bar("fill_default", S["it_fill_default"], "ok",
                          text=S["n_filled"].format(n=f"{int(fdf['added']):,}") if fdf.get("added") is not None
                          else S["done"]))
    elif fdf.get("state") == "opted_out":
        items.append(_bar("fill_default", S["it_fill_default"], "wait", text=S["skipped"]))
    elif fdf.get("state") in ("blocked", "failed", "timeout", "stopped"):
        items.append(_bar("fill_default", S["it_fill_default"], "fail", text=S["skipped"]))
    # the AI's reads and the annual reports fetched on demand
    screen_done = _done(job, "screen")
    for sid, label in (("l1", "it_l1"), ("l2", "it_l2"), ("fetch", "it_fetch")):
        g = stages.get(sid)
        finished = (screen_done and sid in ("l1", "l2")) or (sid == "fetch" and _done(job, "fetch"))
        if g is None and not finished:
            if sid == "fetch":
                continue           # shown only when annual reports are actually fetched
            if st in ("running", "waiting", "new", None) or not screen_done:
                items.append(_bar(sid, S[label], "wait", text=S["waiting"]))
            continue
        if g is None:
            if sid != "fetch":
                items.append(_bar(sid, S[label], "ok", text=S["done"]))
            continue
        done, total = g.get("done"), g.get("total")
        text = S["n_of"].format(done=f"{done:,}" if isinstance(done, int) else "…",
                                total=f"{total:,}" if isinstance(total, int) else "…")
        if finished or (isinstance(done, int) and isinstance(total, int) and total and done >= total
                        and p.get("stage") != sid):
            items.append(_bar(sid, S[label], "ok", done=done, total=total, text=text))
        else:
            running = st == "running" and p.get("stage") == sid
            items.append(_bar(sid, S[label], "run" if running else "wait", done=done, total=total, text=text,
                              eta=eta_words(_stage_eta(g, now), lang) if running else None))
    fill = job.get("fill") or {}
    if fill.get("answer") == "yes" and st == "running":
        items.insert(0, _bar("fill", S["it_fill"], "run", text=S["running"]))
    spent = _spent(job)
    money = None
    if spent is not None:
        cap = float((job.get("approval") or {}).get("usd") or 0.0)
        x, cny = money_words(spent, lang)
        money = (S["spent"].format(x=x, cny=cny, cap=f"{cap:.2f}") if cap else S["spent_nocap"].format(x=x, cny=cny))
        if lang == "zh" and not cny:
            money = money.replace("（）", "")
    if money is None and all(it["state"] == "wait" for it in items):
        return None                  # nothing has started: the checklist says what comes first
    hb = p.get("heartbeat_at") or (job.get("worker") or {}).get("heartbeat_at")
    return {"items": items, "money": money, "updated": S["updated"].format(at=when_words(hb, lang)) if hb else None}


def _alerts(cfg, job: dict[str, Any] | None, lang: str) -> list[dict[str, Any]]:
    """Blocks, cooldowns and stops in plain words (shown in red); none of them is a key problem (the key line
    says those)."""
    from . import guard, quickstart
    Q = quickstart.STRINGS[lang]
    S = T[lang]
    out: list[dict[str, Any]] = []
    shown_cool: set[str] = set()
    failure = (job or {}).get("failure") or {}
    kind = failure.get("kind")
    st = (job or {}).get("state")
    ra = when_words(failure.get("retry_after"), lang) if failure.get("retry_after") else "?"
    if st == "failed" and kind == "blocked":
        if failure.get("source") == "fd":
            out.append({"kind": "block", "text": Q["fd_blocked"].format(retry_after=ra, **quickstart._fd_urls(lang))})
            shown_cool.add("fetch-fd")
        else:
            out.append({"kind": "block", "text": Q["tv_blocked"].format(retry_after=ra)})
            shown_cool.add("refresh-universe")
    elif st == "failed" and kind == "network":
        out.append({"kind": "block", "text": Q["net_down"].format(t=ra)})
    elif st == "failed" and kind == "fd":
        out.append({"kind": "block", "text": Q["fd_failed"].format(**quickstart._fd_urls(lang))})
    elif st == "failed" and kind == "busy":
        out.append({"kind": "note", "text": Q["store_busy"]})
    elif st == "failed" and kind == "python":
        out.append({"kind": "block", "text": Q["python_old"]})
    elif st == "failed" and kind == "failed":
        reason = failure.get("reason")
        key = {"duckdb": "fail_duckdb", "consent": "fail_consent", "canary": "fail_canary", "results": "fail_results",
               "screen_args": "fail_screen_args"}.get(reason or "")
        out.append({"kind": "block", "text": Q[key] if key else Q["failed"].format(error=failure.get("error") or "?")})
    elif st == "interrupted":
        out.append({"kind": "block", "text": Q["interrupted"]})
    elif st == "declined":
        out.append({"kind": "block", "text": Q["declined"]})
    if st == "done" and ((job or {}).get("result") or {}).get("status") == "budget_exhausted":
        n = int((((job or {}).get("result") or {}).get("summary") or {}).get("listed") or 0)
        out.append({"kind": "block", "text": Q["budget_exhausted"].format(n=n, x=_usd((job or {}).get("topup_usd")
                                                                                     or 0.25))})
    for cmd in COOL_ORDER:
        if cmd in shown_cool:
            continue
        hit = guard.recent_block_marker(cfg, cmd)
        if hit is None:
            continue
        at = hit["blocked_at"]
        until = at + dt.timedelta(hours=24)
        out.append({"kind": "cooldown", "text": S["cool"].format(src=COOL_SOURCES[cmd][0 if lang == "zh" else 1],
                                                                 at=when_words(at, lang),
                                                                 until=when_words(until, lang))})
    return out


def review_overdue(job: dict[str, Any] | None, now: dt.datetime | None = None) -> bool:
    """Part A is still pending but older than review.AGENT_REVIEW_TIMEOUT_S: the user's AI stopped (the page gives
    up waiting even when nobody polls the status any more)."""
    from . import review
    ar = (job or {}).get("agent_review") or {}
    t = _parse(ar.get("created_at"))
    if ar.get("state") != "pending" or t is None:
        return False
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    return (now - t).total_seconds() >= review.AGENT_REVIEW_TIMEOUT_S


def review_pending(job: dict[str, Any] | None, now: dt.datetime | None = None) -> bool:
    """The user's AI is reviewing the first result (scope design §7.3: agent_review part A pending, not overdue)."""
    return ((job or {}).get("agent_review") or {}).get("state") == "pending" and not review_overdue(job, now)


def _phase(job: dict[str, Any] | None, lang: str, result: dict[str, Any] | None,
           checks: list[dict[str, Any]]) -> tuple[str, str]:
    """(phase id, the words of the status pill)."""
    S = T[lang]
    if job is None:
        if result is None:
            return "fresh", S["ph_fresh"]
        rs = result.get("status")
        return "done", S["ph_partial"] if rs == "partial" else S["ph_budget"] if rs == "budget_exhausted" else \
            S["ph_done"]
    st = job.get("state")
    failure = job.get("failure") or {}
    if st == "declined":
        return "declined", S["ph_declined"]
    if st == "done":
        rs = (job.get("result") or {}).get("status")
        if job.get("queued"):
            return "busy", S["ph_busy"]
        if review_pending(job):
            return "review", S["ph_review"]
        return "done", S["ph_partial"] if rs == "partial" else S["ph_budget"] if rs == "budget_exhausted" else \
            S["ph_done"]
    if st == "failed":
        k = failure.get("kind")
        if k in ("blocked", "network", "fd"):
            return "blocked", S["ph_blocked"]
        if k == "ai_unavailable":
            return "key", S["ph_key"]
        if k == "busy":
            return "busy", S["ph_busy"]
        return "failed", S["ph_failed"]
    if st == "interrupted":
        return "failed", S["ph_stopped"]
    if st == "running":
        if (job.get("fill") or {}).get("answer") == "yes" and job.get("result"):
            return "fill", S["ph_fill"]
        cs = job.get("current_step")
        return {"check": ("check", S["ph_check"]), "universe": ("download", S["ph_download"]),
                "descriptions": ("download", S["ph_download"]), "pack": ("download", S["ph_download"]),
                "idea_en": ("prepare", S["ph_prepare"]), "ai_check": ("prepare", S["ph_prepare"]),
                "estimate": ("prepare", S["ph_prepare"]), "screen": ("screen", S["ph_screen"]),
                "fetch": ("fetch", S["ph_fetch"]), "finish": ("finish", S["ph_finish"])}.get(cs or "",
                                                                                          ("check", S["ph_check"]))
    if job.get("queued"):
        return "busy", S["ph_busy"]
    if any(c["state"] in ("need", "fail") for c in checks):
        return "wait_you", S["ph_wait_you"]
    if not job.get("idea_en"):
        return "wait_ai", S["ph_wait_ai"]
    return "fresh", S["ph_fresh"]


# ------------------------------------------------------------------------------------------------ the block

def build(cfg, *, lang: str, job: dict[str, Any] | None = None, result: dict[str, Any] | None = None,
          now: dt.datetime | None = None) -> dict[str, Any]:
    """The status block of the page (data['status']) in `lang`. `job`: the quickstart job of the idea (None: the
    page of a plain `screen` / `answer` / `page` run); `result`: the page's result data when it has one ({run_id,
    status, funnel})."""
    lang = _lang(lang)
    S = T[lang]
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    from . import quickstart
    stale = None
    if (job or {}).get("state") == "running":
        # the worker beats every few seconds and rewrites this page each time; a beat older than HEARTBEAT_STALE_S
        # means it was killed (sleep, crash): say it stopped instead of showing work in progress
        hb = _parse((job.get("worker") or {}).get("heartbeat_at"))
        if hb is not None and (now - hb).total_seconds() > quickstart.HEARTBEAT_STALE_S:
            job = {**job, "state": "interrupted"}
        elif hb is not None:
            # nobody rewrites the page once the worker is dead: the page itself notices a beat that got old
            stale = {"since": hb.replace(microsecond=0).isoformat() + "Z", "after_s": quickstart.HEARTBEAT_STALE_S,
                     "phase_words": S["ph_stopped"], "text": quickstart.STRINGS[lang]["interrupted"]}
    v, py_ok, duck_ok = _install()
    checks: list[dict[str, Any]] = []
    if not py_ok:
        checks.append(_check("python", "fail", S["py_old"].format(v=v), S["py_old_fix"]))
    elif not duck_ok:
        checks.append(_check("python", "fail", S["duck_missing"], S["duck_fix"]))
    else:
        checks.append(_check("python", "ok", S["py_ok"].format(v=v)))
    c = quickstart.consent_state(cfg)
    if quickstart.consent_current(c):
        checks.append(_check("consent", "ok", S["consent_ok"]))
    elif c["state"] == "no":
        checks.append(_check("consent", "fail", S["consent_no"], S["consent_no_fix"]))
    elif result is not None and job is None:
        checks.append(_check("consent", "ok", S["consent_ok"]))
    else:
        checks.append(_check("consent", "need", S["consent_need"], S["consent_need_fix"]))
    result_ok = result is not None and result.get("status") in ("ok", "partial", "budget_exhausted")
    key, shown = _key_check(cfg, job, lang, result_ok)
    checks.append(key)
    checks += _data_checks(job, lang, result)
    checks.append(_ready_check(job, lang, result))
    all_ok = all(ch["state"] == "ok" for ch in checks)
    phase, words = _phase(job, lang, result, checks)
    st = (job or {}).get("state")
    failure = (job or {}).get("failure") or {}
    # auto-refresh while something can still change: running, waiting for an answer or a key; never on a finished,
    # declined or blocked (24 h) job, nor on a page without a job
    refresh = job is not None and st not in ("done", "declined") and not (
        st == "failed" and failure.get("kind") == "blocked")
    if st == "done" and (job.get("fill") or {}).get("answer") == "yes" and not (job.get("fill") or {}).get(
            "finished_at"):
        refresh = True
    alerts = _alerts(cfg, job, lang)
    if review_pending(job, now):
        refresh = True             # the user's AI is reviewing: the page updates itself when it is done
        alerts = [{"kind": "note", "text": S["review_banner"]}] + alerts
    elif review_overdue(job, now):
        alerts = [{"kind": "note", "text": S["review_over"]}] + alerts
    show_progress = job is not None and (st != "done" or phase == "fill")
    return {"lang": lang, "phase": phase, "phase_words": words, "refresh": bool(refresh),
            "checks": checks, "optional": _optional(cfg, lang), "all_ok": all_ok,
            "ok_line": (S["all_ok"].format(p=shown) if shown else S["all_ok_nokey"]) if all_ok else None,
            "progress": _progress(_with_fill_file(cfg, job), lang, now) if show_progress else None,
            "alerts": alerts, "stale": stale}


def text_lines(status: dict[str, Any] | None, lang: str) -> list[str]:
    """The status block as plain text (the <noscript> block and `jevscreen page --text`): one line when everything
    is ready, else one line per check with its fix; the progress bars and the alerts."""
    if not status:
        return []
    zh = _lang(lang) == "zh"
    mark = {"ok": "✓", "run": "…", "wait": "○", "need": "✗", "fail": "✗", "opt": "○"}
    out = [f"[{status.get('phase_words')}]"]
    for a in status.get("alerts") or []:
        out.append(("！" if zh else "! ") + str(a.get("text")))
    if status.get("all_ok"):
        out.append("✓ " + str(status.get("ok_line")))
    else:
        for c in status.get("checks") or []:
            fix = c.get("fix")
            out.append(f"{mark.get(c['state'], '?')} {c['text']}" + ((f"（{fix}）" if zh else f" ({fix})") if fix
                                                                    else ""))
    prog = status.get("progress") or {}
    for it in prog.get("items") or []:
        bits = [x for x in (it.get("text"), it.get("eta")) if x]
        pct = f" {it['pct']}%" if it.get("pct") is not None and it["state"] != "ok" else ""
        out.append(f"  {mark.get(it['state'], '?')} {it['label']}{pct}" + ((("：" if zh else ": ") + " · ".join(bits))
                                                                        if bits else ""))
    if prog.get("money"):
        out.append("  " + prog["money"])
    return out


# ------------------------------------------------------------------------------------------------ writing

@contextlib.contextmanager
def page_lock(cfg, idea: str, wait_s: float = 5.0) -> Iterator[bool]:
    """Hold the page lock of an idea (<home>/locks/page-<idea_key>.lock; also between threads of one process) while
    its stable page is read and rewritten; after wait_s without it, go on without (a page write never fails over
    its lock). Yields whether it is held."""
    from . import guard, keywords
    name = f"page-{keywords.idea_key(idea)}"
    deadline = time.monotonic() + wait_s
    held = None
    while held is None:
        cm = guard.budget_lock(cfg, name)
        try:
            cm.__enter__()
            held = cm
        except guard.Busy:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    try:
        yield held is not None
    finally:
        if held is not None:
            held.__exit__(None, None, None)


def write(cfg, job: dict[str, Any], *, warn: Callable[[str], None] | None = None) -> Path | None:
    """Rewrite the stable page of the job's idea with a fresh status block, keeping the results it already holds
    (a page without results yet is created in the job's language). Atomic; scanned for secrets first. Returns the
    path, or None after a warning (a page failure never stops the caller)."""
    from . import ops, page
    try:
        sp = page.stable_path(cfg, job["idea"])
        with page_lock(cfg, job["idea"]):
            data = page.read_page_data(sp) if sp.exists() else None
            if not data or data.get("format") != page.PAGE_FORMAT:
                data = page.shell_data(job["idea"], lang=job.get("lang") or "zh", idea_en=job.get("idea_en"))
            has_result = bool(data.get("run_id"))
            data["live"] = build(cfg, lang=data.get("lang") or "zh", job=job,
                                 result=page.result_facts(data) if has_result else None)
            if not has_result and job.get("idea_en") and data.get("idea_en") != job.get("idea_en"):
                data.update(page.shell_data(job["idea"], lang=data.get("lang") or "zh", idea_en=job["idea_en"],
                                            status=data["live"]))
            html = page.render_page(data)
            hits = ops.scan_secrets(cfg, html)
            if hits:
                raise RuntimeError(f"the page would contain a configured secret ({', '.join(hits)})")
            sp.parent.mkdir(parents=True, exist_ok=True)
            tmp = sp.with_suffix(f".{os.getpid()}.st.tmp")
            tmp.write_text(html, encoding="utf-8")
            os.replace(tmp, sp)
        return sp
    except Exception as e:  # noqa: BLE001 - a page failure is a warning, never an exit code
        (warn or (lambda m: None))(f"warning: page not updated ({type(e).__name__}: {str(e)[:200]})")
        return None
