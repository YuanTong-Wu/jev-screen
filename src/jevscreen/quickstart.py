"""`jevscreen quickstart`: from an idea to a ranked page with evidence, driven by an AI agent in one round of questions.

Execution model (see docs/AGENT_API.md "quickstart"):
- front(): the command the agent runs. It returns in seconds, makes no network request, reads or creates the job
  file <home>/quickstart/<idea_key>.json, applies the flags (--idea-en, --approve-budget, ...), computes `pending`
  (agent item idea_en first, then the human items: consent, budget approval, the Jev key) and, when the worker
  has something it can do, spawns it detached (python -m jevscreen.quickstart --worker <idea_key>) and returns.
- worker(): runs every step it can (check, universe, descriptions, pack, idea_en, ai_check + canary, estimate,
  screen, cards + page) while holding guard.budget_lock(cfg, 'quickstart'), under cli.sigterm_as_interrupt(). It
  NEVER prompts. At a step that needs an open decision it writes state 'waiting' and exits; the free downloads run
  while the human is still creating the key. It ends 'done' or 'failed' ('interrupted' on SIGTERM / Ctrl-C).
- status(): the same JSON from the job file; wait_s blocks until the state or the pending items change (<= 110 s,
  counted from the call). A job no worker runs that waits on nothing any more (the key set after the worker stopped
  at it, flags left in the inbox after its last merge, a queued idea) is started again through the front's own
  bookkeeping. front and status never wait on the DuckDB lock (VIEW_DB_WAIT_S).
- Only the lock holder writes the job file (atomic tmp + os.replace, scanned by ops.scan_secrets first: it never
  holds a key or the SEC name / e-mail); the one exception is the first job file of an idea queued behind another
  idea's worker. A front that finds the lock held leaves its flags in <idea_key>.flags.json for the next lock
  holder to merge and returns 'running' (its own worker) or 'store_busy' (queued). A 'running' job whose heartbeat is older than HEARTBEAT_STALE_S with
  the lock free was killed: it is marked 'interrupted' and a new worker is spawned.
- The download steps go through ops.run_networked under the standard command names (refresh-universe, fetch-fd,
  import-fd, crawl-descriptions: the optional profile fill after the first result, only after the human's yes):
  consent, 24 h cooldown after a block, rate-budget locks and the runs journal are exactly those of the
  plain commands. A network failure without a block is not retried for RETRY_NET_S unless --retry. --after-block is
  never used. Annual reports come only from the fetch step (ondemand_cli.run_fetch, the path of `screen --fetch-docs
  auto`): one child per source (SEC, CNINFO, BSE, MOPS after the mops-annual consent, DART with an OpenDART key)
  runs that adapter's own sync under its lock, limiter, cooldown marker and stop-on-first-block.
- Money (one definition): approval = {usd X, idea_en_sha, approved_at, runs}. remaining = X - (Jev cost of the
  runs made under it + the canary). The worker reads the ledger strictly (a locked store is 'busy', never a guess);
  the views fall back to the worker's last reading. The screen always gets budget_usd = remaining. The first run under an approval
  needs the dry run's reservation <= remaining, else the human gets the estimate and narrower alternatives. A new
  idea_en voids the approval (the human approved the English text they saw). X is the total cap: a top-up passes
  the new total.
- Reuse: a done job with the same idea, idea_en, floor, countries and sieve version returns its result ($0, no run)
  when that result is ok / partial. A budget_exhausted result stays with a pending top-up; a larger total screens
  again (cached answers are free).
- idea_en precedence: --idea-en > sieve.idea_en > the job's pin; a changed sieve idea_en under an approval is the
  reprice question. The front checks --idea-en for company names (calib.idea_en_problems: case-sensitive proper
  nouns, a single ordinary English word such as 'Core' never counts) against the store or the names cache
  (<home>/quickstart/names.json, written right after every stock-list download). Only on the very first run is there
  nothing to check against: the worker checks right after the stock list is in (after_universe), while the free
  downloads go on. A refused sentence comes back with suggested_idea_en and rerun_command on every answer of the
  front; an approval the human gave for it is held: only a recase or a Core/Main-style synonym of a flagged ordinary
  word keeps it (calib.idea_en_minor_fix; noted, never asked again), any other new sentence is the reprice question.
  A company the idea names in Chinese / Japanese / Korean may be named in English (calib.IDEA_EN_ALIASES).
- One page per idea: result.page is always <home>/pages/<idea_key>.html, and when a newer run of the idea owns it
  (card answers, fetch-docs, a plain screen) the views show that run (newest_view). Time and cost are the idea's
  totals (idea_totals: the worker's own clock plus runs made outside it; every Jev cost of its runs + the key test).
- The optional profile fill: after the first result, when the idea's market has a large profile hole (fill_offer),
  one optional question (pending fill_descriptions, status stays done) naming missing companies of the passes'
  industries (fill_examples). A yes runs crawl-descriptions for that market at the run's floor, then screen
  --from-run with l1_new (only the newly described are read by L1), the fetch step and the page again; the first
  result stays in the job meanwhile. A yes given while another worker holds the lock waits in the inbox (status
  store_busy with poll_command) and --status starts it once the lock is free. Every ending of the fill is told.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

FORMAT = "jevscreen.quickstart/1"
LOCK = "quickstart"
HEARTBEAT_STALE_S = 60.0
HEARTBEAT_EVERY_S = 10.0
PROGRESS_EVERY_S = 10.0
RETRY_NET_S = 15 * 60
WAIT_DEFAULT_S, WAIT_MAX_S = 100.0, 110.0
RELAY_EVERY_S = 60
DEFAULT_MIN_MCAP = 1e9
DEFAULT_APPROVAL_USD = 1.0
READS = 3
CARDS = 8
CANARY_BUDGET_USD = 0.001
UNIVERSE_FRESH_DAYS = 7
DESC_IMPORT_FRESH_DAYS = 30
DESC_OK_SHARE = 0.60
DESC_FAIL_SHARE = 0.30
UNCERTAIN_AUTO_USD = 0.01
UNCERTAIN_ITEM_USD = 0.00005        # upper bound per resent item (L1 ~$0.000033, L2 ~$0.0000446)
FETCH_DOCS_S = 150.0                # on-demand annual reports after the screen (ondemand_cli.run_fetch --fetch-time)
VIEW_DB_WAIT_S, VIEW_DB_POLL_S = 0.3, 0.1   # front / status: never wait on the DuckDB lock (they return in < 5 s)
WORKER_DB_WAIT_S = 60.0                     # the worker: reads the money ledger with a long wait, never guesses
CNY_PER_USD = 7.2
FILL_MIN_SHARE = 0.30        # the fill question: >= 30% of the idea's market has no profile at all ...
FILL_TOP, FILL_MIN_TOP = 100, 3      # ... or >= 3 of its 100 largest companies have none
FILL_NAMES = 5               # the largest missing companies named in the question
FILL_ITEM_USD = 0.00005      # re-rank estimate per newly described company (L1 ~$0.000033 + a share of L2 reads)
FILL_MARKET_WORDS = {"CN": ("中国", "Chinese"), "HK": ("香港", "Hong Kong"), "TW": ("台湾", "Taiwanese"),
                     "JP": ("日本", "Japanese"), "KR": ("韩国", "Korean"), "IN": ("印度", "Indian")}
EXPECTED_RANGE = (0.2, 0.4)

EXIT_OK, EXIT_FAILED, EXIT_BLOCKED, EXIT_BUSY_STORE = 0, 1, 2, 3
EXIT_BUDGET, EXIT_AI = 5, 6
EXIT_NEEDS_HUMAN, EXIT_NEEDS_AGENT, EXIT_DECLINED = 10, 11, 12
STATUS_EXIT = {"running": 0, "done": 0, "partial": 0, "failed": 1, "blocked": 2, "store_busy": 3,
               "budget_exhausted": 5, "ai_unavailable": 6, "needs_human": 10, "needs_agent": 11, "declined": 12}
STEPS = ("check", "universe", "descriptions", "pack", "idea_en", "ai_check", "estimate", "screen", "fetch",
         "finish")
FREE_STEPS = ("check", "universe", "descriptions", "pack")
PHASE_OF = {"check": 1, "universe": 2, "descriptions": 2, "pack": 2, "idea_en": 3, "ai_check": 3, "estimate": 3,
            "screen": 4, "fetch": 4, "finish": 5}
PHASE_ZH = {1: "检查", 2: "下载数据", 3: "准备", 4: "AI 筛选", 5: "生成结果页"}
PHASE_EN = {1: "Check", 2: "Download data", 3: "Prepare", 4: "AI screen", 5: "Results page"}

# ------------------------------------------------------------------------------------------------ texts (zh / en)

STRINGS: dict[str, dict[str, str]] = {
    "zh": {
        "before": "开始前说明：①会问你可不可以用两个数据源（TradingView 的股票清单、Yahoo 的公司简介）：它们的条款限制程序取用，"
                  "只能你个人研究用、不能分享；②要一个能付费用 Jev（读简介和年报的 AI 服务）的账号：TypeSafe 官方 API、OpenRouter "
                  "或 Vercel AI Gateway 任选一家，价格一样。第一次一般要先充值（最低充值额通常是几美元，另有支付手续费，以各家"
                  "付款页为准），需要能付美元的卡；这次筛选只从余额里扣约 $0.3（约 ¥2），剩下的留着下次用；③第一次约 15–25 分钟"
                  "（含注册账号）。Mac 第一次用可能弹出「安装命令行开发者工具」，点安装即可（几分钟）。你只需要回答一轮问题。",
        "intro": "接下来：先下载股票清单和公司简介（免费，约 1 分钟），再让 AI 读这些简介、用年报核对（约 3 分钟，约 $0.3），"
                 "最后在浏览器里给你一个排好序的名单，每家都附原文证据。",
        "bg_started": "免费下载已经在后台开始了，你准备 key 的时候不耽误。",
        "need_dk": "需要你决定{n}件事，并设置一个 key：", "need_d": "需要你决定{n}件事：", "need_k": "还需要你设置一个 key：",
        "budget_q": "我会这样理解你的想法（发给 AI 的英文）：'{idea_en}'。这次最多花 ${x}（约 ¥{y}）可以吗？预计约 ${est}。"
                    "如果中途中断，重试可能多花不到 1 美分。开始前会先用约 $0.0001 试一下 key 能不能付费。",
        "budget_topup_q": "预算用完了：结果页先给出已核对的部分。继续需要再同意约 ${more}，也就是把总上限提高到 ${total}。可以吗？",
        "over_q": "预计要预留 ${r}，超过你同意的 ${x}。可以缩小范围：{alts}；或者明确同意一个更高的上限，比如 ${r_up}。",
        "uncertain_q": "上次中断时有 {n} 条请求结果不明，重发约 ${c}（超过 1 美分）。同意把总上限提高到 ${total} 吗？",
        "reprice_q": "发给 AI 的英文要从 '{old}' 改成 '{new}'。这会重新计费（AI 初读约 ${l1}）。同意改用新的英文，并重新确认"
                     "预算上限 ${x} 吗？不同意就继续用原来的英文。",
        "key": "请在 {key_url} 创建一个 {label} 的 key{limit}。我会弹出一个输入框让你粘贴，"
               "输入内容不会显示，也不会经过我。弹不出来的话：{terminal}，运行 `{abs_cmd}`，粘贴后按回车。千万不要把 key 发到聊天里。",
        "key_limit": "（建议给它设几美元的额度上限）",
        "key_credits": "（先在 {credits_url} 买一点 AI Gateway credits，Jev 可能不在免费额度里）",
        "account_q": "Jev（读简介和年报的 AI 服务）要用你自己的付费账号。下面三家卖的是同一个 Jev，价格一样（这次约 $0.3）。"
                     "你有哪一家的账号？"
                     "① TypeSafe 官方（Jev 的开发公司）：在 {ts_signup} 注册（官方注册有时会暂停），然后在 {ts_keys} 创建 key；"
                     "② OpenRouter：在 {or_signup} 注册并充值几美元，然后在 {or_keys} 创建 key（建议给 key 设几美元的额度上限）；"
                     "③ Vercel：在 {vc_signup} 注册，在 AI Gateway 买一点 credits（Jev 可能不在免费额度里），然后在 AI Gateway 的 "
                     "API Keys 页面 {vc_keys} 点 Create key。"
                     "都没有的话，OpenRouter 最省事。告诉我是哪一家；key 准备好了说一声，我会弹出一个输入框让你粘贴，输入内容不会显示，"
                     "也不会经过我。千万不要把 key 发到聊天里。",
        "account_fallback": "输入框弹不出来的话：{terminal}，运行你那一家对应的命令：{cmds}，粘贴后按回车。",
        "key_other": "想改用另一家的账号？设置那一家的 key 就会改用它（{cmds}）。",
        "declined": "你选择了不使用这些来源。目前筛选离不开它们，所以先停在这里，不会再问。改主意了？告诉你的 AI『我同意使用这些来源』，"
                    "它会重新记录。",
        "running": "[{phase}/5] {what}…",
        "p_check": "检查安装", "p_check_ok": "[1/5] 检查安装…好",
        "p_universe": "下载股票清单（TradingView）",
        "p_universe_ok": "[2/5] 下载股票清单（TradingView）…{n} 家市值 ≥ {floor}，{s} 秒",
        "p_universe_skip": "[2/5] 股票清单是 {age} 天内的，不用重新下载",
        "p_desc": "下载公司简介（FinanceDatabase，15 MB）",
        "p_desc_ok": "[2/5] 下载公司简介（FinanceDatabase，15 MB）…{n} 家有简介（{pct}%），{s} 秒",
        "p_desc_skip": "[2/5] 公司简介已经够用（{pct}% 有简介），不用重新下载",
        "p_pack": "日本年报数据包", "p_pack_skip": "[2/5] 没有配置开放数据包：日本公司先用简介",
        "p_ai": "用约 $0.0001 试一下 key 能不能付费", "p_ai_ok": "[3/5] 用约 $0.0001 试一下 key 能不能付费…可以",
        "p_estimate": "估算花费", "p_estimate_ok": "[3/5] 估算：预计约 ${est}，最多预留 ${r}",
        "p_l1": "[4/5] AI 初读 {done}/{total} 家简介…", "p_fetch": "[4/5] 补年报原文 {done}/{total}（上限 {s} 秒）…",
        "p_l2": "[4/5] AI 核对 {done}/{total} 家…", "p_finish": "[5/5] 生成结果页…",
        "done": "完成：{n} 家入选（年报确认 {a}，只有简介 {p}）。这个想法从你同意起共用时 {t}（不算等你回答的时间），"
                "共花费 ${c}（{y}）。结果页：{where}。仅供你个人研究，请勿分享。",
        "done_version": "这是第 {v} 版：{change}。",
        "fill_q": "可选，问你一件事：这次有 {n} 家{market}公司（市值 ≥ {floor}，占{market}公司的 {pct}%）没有公司简介，AI 没读到"
                  "{names}。要补吗？补简介免费、在后台跑，约 {m} 分钟；补完只让 AI 初读新补的这些公司并重新排序，"
                  "约 ${est}（从你已同意的 ${x} 里扣，剩 ${rem}）。说“不用”也行，不影响现在这份结果。",
        "fill_names_related": "，其中和入选公司同一行业的有 {names}",
        "fill_names_largest": "，其中市值较大的有 {names}",
        "fill_running": "[补简介] 正在后台补{market}公司的简介（约 {n} 家，约 {m} 分钟，免费）…",
        "fill_ok": "[补简介] 补到 {k} 家公司的简介，{s} 秒",
        "fill_screen": "[补简介] AI 初读新补的 {n} 家公司并重新排序…",
        "fill_none": "[补简介] 没有补到新的简介，结果不变",
        "fill_blocked": "补简介时 TradingView 暂时拒绝了请求：24 小时内不再请求它（{retry_after} 后可再试），现在这份结果不变。",
        "fill_rerank_budget": "[补简介] 补到 {k} 家公司的简介，但预算不够，重新排序没做完，现在这份结果不变"
                              "（AI 初读新公司已花 ${c}，从你同意的预算里扣）。",
        "fill_rerank_busy": "[补简介] 补到 {k} 家公司的简介，但数据库正忙，没来得及重新排序，现在这份结果不变；"
                            "以后重新筛选这个想法时会用上它们。",
        "fill_rerank_failed": "[补简介] 补到 {k} 家公司的简介，但重新排序出错了，现在这份结果不变；以后重新筛选这个想法时会用上它们。",
        "fill_queued": "你说要补简介，已经记下。另一个任务正在后台用数据库，补简介排在它后面，它一结束就开始（补简介约 {m} 分钟）。"
                       "现在这份结果不变；过几分钟查一下进度就行。",
        "change_fill": "补了 {n} 家公司简介后重排",
        "change_fetch": "补抓 {n} 家年报后重排",
        "change_answers": "应用了你的 {n} 个回答后重排",
        "change_rerun": "重新筛选",
        "optional_q": "（可选，直接说“不用”就行，不影响这次结果）",
        "idea_en_fix": "发给 AI 的英文从 '{old}' 改成了 '{new}'：原句里的 {words} 被误当成公司名，只改了这个词，意思没变，"
                       "你同意的预算照旧。",
        "idea_en_refused_q": "原来那句英文 '{old}' 不能用：{why}。新的英文是 '{new}'，意思有变化。同意改用新的英文吗？"
                             "预算上限仍是你同意的 ${x}。不同意的话，我再写一句。",
        "opened": "已在浏览器打开", "open_file": "用浏览器打开这个文件：{uri}",
        "top_line": "{rank}. {name}（{ticker}，{country}）——{one}；{verdict}，{evidence}",
        "ev_annual_report": "年报原文", "ev_profile": "只有简介",
        "ev_gap_annual_report": "年报摘录未提到（缺口）", "ev_gap_profile": "简介摘录未提到（缺口）",
        "ev_user": "按你的判断（AI 没从原文确认）", "mark_user": "，你的判断", "mark_edge": "，边缘",
        "next_idea": "换一个想法：直接告诉你的 AI（约 4 分钟，约 $0.25，不用再下载）",
        "next_cards": "想更准：在结果页回答几张卡（可选；应用后重新排序，约 $0.01–0.03）",
        "next_gap_cn": "补中国公司简介：约 {n} 家，约 {m} 分钟，可后台",
        "next_gap_sec": "用美股年报原文核对：下载时按 SEC 规则附上一个名字和邮箱（可选，不用注册账号）",
        "budget_exhausted": "预算用完了：结果页先给出已核对的 {n} 家。继续需要再同意约 ${x}。",
        "fd_failed": "公司简介下载不下来（GitHub 在你的网络上可能被限速或无法访问）。15 分钟后可以加 --retry 再试；或者用浏览器下载 "
                     "{url}（打不开就试 {url2}），再让你的 AI 用 --fd-file <下载的文件> 重跑。",
        "fd_blocked": "公司简介的下载地址暂时拒绝了我们的请求。为了守规矩，24 小时内不再请求它们（{retry_after} 后可再试）。不想等："
                      "用浏览器下载 {url}（打不开就试 {url2}），再让你的 AI 用 --fd-file <下载的文件> 重跑。",
        "tv_blocked": "TradingView 暂时拒绝了我们的请求。为了守规矩，24 小时内不再请求它（{retry_after} 后可再试）。",
        "no_credit": "{label} 说账户没有余额。请去 {credits_url} 充值，然后告诉我『好了』。",
        "key_rejected": "{label} 不接受这个 key，请重新创建并设置。",
        "key_rejected_head": "{label} 不接受这个 key。",
        "key_rejected_vercel": "Vercel 不接受这个 key 用 Jev（HTTP 403）：多半是还没买 AI Gateway credits（Jev 可能不在免费额度里），"
                               "或者 key 不对。请在 {credits_url} 买一点 credits，或重新创建 key 并设置。",
        "store_busy": "另一个 jev-screen 任务正在用数据库。过几分钟重跑同一条命令就会接着做（做完的步骤不会重做）。",
        "queued": "另一个想法的筛选正在后台运行，这个想法排在它后面。你的回答已经记下；过几分钟查一下进度或重跑同一条命令，"
                  "它就会开始。",
        "net_down": "网络连不上，{t} 后再试。",
        "python_old": "这台电脑的 Python 太旧。最简单的办法：安装 uv（一条命令），它会自带合适的 Python。",
        "failed": "后台任务出错停下了。重跑同一条命令会从断点继续；如果还是出错，把这段技术信息给你的 AI 看：{error}",
        "fail_duckdb": "缺少数据库组件 duckdb。让你的 AI 按安装步骤重新安装（pip install -e '.[pdf]'），然后重跑同一条命令。",
        "fail_consent": "还没有记录你对两个数据源的回答。回答那个问题（可以 / 不要）后，重跑同一条命令。",
        "fail_canary": "用来测试 key 能不能付费的那一次小请求没有成功（不是余额或 key 的问题）。过几分钟重跑同一条命令再试。",
        "fail_results": "筛选做完了，但结果暂时读不出来（数据库可能正被别的任务占用）。过几分钟重跑同一条命令，不会重复收费。",
        "fail_screen_args": "筛选的设置有问题，没有发出任何请求、没有花钱。让你的 AI 看一下 error 字段里的原因并改正后重跑。",
        "reused": "这个想法已经筛选过、条件没变：直接给你上次的结果（没有花钱）。",
        "interrupted": "后台任务中断了：重跑同一条命令会从断点继续（已付费的结果不会重复收费）。",
    },
    "en": {
        "before": "Before we start: (1) I will ask if you are OK with two data sources (TradingView's stock list, "
                  "Yahoo company profiles) whose terms restrict automated use: for your personal research only, never "
                  "shared; (2) you "
                  "need an account that pays for Jev (the AI service that reads profiles and annual reports): TypeSafe's "
                  "official API, OpenRouter or Vercel AI Gateway, whichever you have; the price is the same. The first "
                  "top-up usually has a minimum (a few dollars, plus a payment fee; see the provider's payment page) and "
                  "needs a card that pays in US dollars; this screen uses about $0.30 of it and the rest stays for later. "
                  "(3) About 15–25 minutes the first time, including the sign-up. On a Mac, a box may offer to install "
                  "the command line developer tools: click Install (a few minutes). You answer one round of questions.",
        "intro": "Next: download the stock list and company profiles (free, about 1 min), have an AI read them and check "
                 "against annual reports (about 3 min, about $0.30), then show you a ranked list with evidence in your "
                 "browser.",
        "bg_started": "The free downloads have already started in the background; setting up the key does not hold "
                      "them up.",
        "need_dk": "{N} decision{s} and one key are needed:", "need_d": "{N} decision{s} {are} needed:",
        "need_k": "One key is still needed:",
        "budget_q": "I will put your idea to the AI as: '{idea_en}'. OK to spend up to ${x} on it? Expected about ${est}. "
                    "If the run is interrupted, retrying may cost under 1 cent extra. First, a tiny paid request (about "
                    "$0.0001) checks that the key can pay.",
        "budget_topup_q": "The budget ran out: the page shows what was checked so far. Continuing needs about ${more} more, "
                          "i.e. a total cap of ${total}. OK?",
        "over_q": "This needs a reservation of ${r}, above the ${x} you approved. Narrow it: {alts}; or explicitly approve "
                  "a higher cap such as ${r_up}.",
        "uncertain_q": "{n} requests had an unknown outcome when the run stopped; resending costs about ${c} (over 1 "
                       "cent). OK to raise the total cap to ${total}?",
        "reprice_q": "The English sent to the AI would change from '{old}' to '{new}'. That is charged again (first read "
                     "about ${l1}). Switch to the new English and confirm the budget cap of ${x} again? If not, the old "
                     "English stays.",
        "key": "Create an API key for {label} at {key_url}{limit}. I will open a box where you paste it; the text stays hidden "
               "and never passes through me. If no box appears: {terminal} and run `{abs_cmd}`, paste, press Enter. "
               "Never paste the key into the chat.",
        "key_limit": " (a limit of a few dollars on the key is a good idea)",
        "key_credits": " (first buy some AI Gateway credits at {credits_url}; Jev may not be in the free tier)",
        "account_q": "Jev (the AI service that reads profiles and annual reports) runs on your own paid account. These "
                     "three sell the same Jev at the same price (about $0.30 for this screen). Which one do you have? "
                     "(1) TypeSafe, the official API (Jev's maker): sign up at {ts_signup} (sign-ups are sometimes "
                     "paused), then create a key at {ts_keys}; "
                     "(2) OpenRouter: sign up at {or_signup} and add a few dollars of credit, then create a key at "
                     "{or_keys} (a limit of a few dollars on the key is a good idea); "
                     "(3) Vercel: sign up at {vc_signup}, buy some AI Gateway credits (Jev may not be in the free tier), "
                     "then click Create key on the AI Gateway API Keys page {vc_keys}. "
                     "None yet? OpenRouter is the quickest. Tell me which one; when the key is ready, say so and I will "
                     "open a box where you paste it: the text stays hidden and never passes through me. Never paste the "
                     "key into the chat.",
        "account_fallback": "If no box appears: {terminal} and run the command for your provider: {cmds}; paste, press "
                            "Enter.",
        "key_other": "Using another account instead? Setting its key switches to it ({cmds}).",
        "declined": "You chose not to use these sources. Screening needs them today, so we stop here and will not ask "
                    "again. Changed your mind? Tell your AI 'I agree to use these sources' and it will record that.",
        "running": "[{phase}/5] {what}…",
        "p_check": "Checking the install", "p_check_ok": "[1/5] Checking the install… ok",
        "p_universe": "Downloading the stock list (TradingView)",
        "p_universe_ok": "[2/5] Downloading the stock list (TradingView)… {n} with market cap ≥ {floor}, {s} s",
        "p_universe_skip": "[2/5] The stock list is less than {age} days old; no download needed",
        "p_desc": "Downloading company profiles (FinanceDatabase, 15 MB)",
        "p_desc_ok": "[2/5] Downloading company profiles (FinanceDatabase, 15 MB)… {n} described ({pct}%), {s} s",
        "p_desc_skip": "[2/5] Company profiles are good enough ({pct}% described); no download needed",
        "p_pack": "Japanese filing pack", "p_pack_skip": "[2/5] No open data pack configured: Japan uses profiles",
        "p_ai": "Testing the key with a tiny paid request (about $0.0001)",
        "p_ai_ok": "[3/5] Testing the key with a tiny paid request (about $0.0001)… ok",
        "p_estimate": "Estimating the cost", "p_estimate_ok": "[3/5] Estimate: about ${est}, reserving up to ${r}",
        "p_l1": "[4/5] AI first read {done}/{total} profiles…",
        "p_fetch": "[4/5] Fetching annual reports {done}/{total} (at most {s} s)…",
        "p_l2": "[4/5] AI check {done}/{total} companies…", "p_finish": "[5/5] Building the results page…",
        "done": "Done: {n} companies listed ({a} confirmed by annual report, {p} by profile only). This idea took {t} in "
                "total since you agreed (not counting time waiting for your answers) and cost ${c} in total. Results "
                "page: {where}. For your personal research only; please do not share.",
        "done_version": "This is version {v}: {change}.",
        "fill_q": "Optional question: {n} {market} companies (market cap ≥ {floor}, {pct}% of {market} companies) have no "
                  "company profile, so the AI could not read them{names}. Fill them in? Filling is "
                  "free and runs in the background, about {m} min; then the AI reads only the newly filled companies "
                  "and re-ranks, about ${est} (out of the ${x} you approved; ${rem} left). 'No' is fine too; the "
                  "current result stays as it is.",
        "fill_names_related": "; those in the same industries as the listed companies include {names}",
        "fill_names_largest": "; larger ones include {names}",
        "fill_running": "[Fill profiles] Filling {market} company profiles in the background (about {n} companies, about "
                        "{m} min, free)…",
        "fill_ok": "[Fill profiles] Got profiles for {k} companies, {s} s",
        "fill_screen": "[Fill profiles] AI first read of the {n} newly filled companies, then re-ranking…",
        "fill_none": "[Fill profiles] No new profiles came in; the result is unchanged",
        "fill_blocked": "TradingView refused the profile requests for now: we will not ask it again for 24 hours "
                        "(retry after {retry_after}). The current result stays as it is.",
        "fill_rerank_budget": "[Fill profiles] Got profiles for {k} companies, but the budget ran out before the "
                              "re-ranking finished; the current result stays as it is (the AI's first read of the new "
                              "companies cost ${c}, out of your approved budget).",
        "fill_rerank_busy": "[Fill profiles] Got profiles for {k} companies, but the database was busy, so nothing was "
                            "re-ranked; the current result stays as it is. A later screen of this idea uses them.",
        "fill_rerank_failed": "[Fill profiles] Got profiles for {k} companies, but the re-ranking failed; the current "
                              "result stays as it is. A later screen of this idea uses them.",
        "fill_queued": "Your yes to filling the profiles is saved. Another task is using the database in the background; "
                       "the fill starts right after it (about {m} min). The current result stays as it is; check the "
                       "progress in a few minutes.",
        "change_fill": "re-ranked after filling profiles of {n} companies",
        "change_fetch": "re-ranked after fetching {n} annual reports",
        "change_answers": "re-ranked with your {n} answers",
        "change_rerun": "screened again",
        "optional_q": " (optional: just say no, it does not change this result)",
        "idea_en_fix": "The English sent to the AI changed from '{old}' to '{new}': {words} in the first sentence was "
                       "mistaken for a company name. Only that word changed, the meaning is the same, and your budget "
                       "approval stands.",
        "idea_en_refused_q": "The earlier English '{old}' cannot be used: {why}. The new English is '{new}', which "
                             "changes the meaning. OK to use the new English? The cap stays at the ${x} you approved. "
                             "If not, I will write another sentence.",
        "opened": "opened in your browser", "open_file": "open this file in a browser: {uri}",
        "top_line": "{rank}. {name} ({ticker}, {country}) - {one}; {verdict}, {evidence}",
        "ev_annual_report": "annual report", "ev_profile": "profile only",
        "ev_gap_annual_report": "the filing excerpt does not mention it (gap)",
        "ev_gap_profile": "the profile excerpt does not mention it (gap)",
        "ev_user": "your call (the AI did not confirm it from the text)", "mark_user": ", your call",
        "mark_edge": ", borderline",
        "next_idea": "Try another idea: just tell your AI (about 4 min, about $0.25, no downloads)",
        "next_cards": "Sharpen it: answer a few cards on the page (optional; re-ranking costs about $0.01–0.03)",
        "next_gap_cn": "Fill Chinese company profiles: about {n} companies, about {m} min, can run in the background",
        "next_gap_sec": "Check US companies against annual reports: give the SEC a contact name and e-mail, sent with "
                        "each download (optional, no account)",
        "budget_exhausted": "The budget ran out: the page shows the {n} companies checked so far. Continuing needs about "
                            "${x} more, with your approval.",
        "fd_failed": "Could not download company profiles (GitHub may be slow or blocked on your network). Retry in 15 "
                     "minutes with --retry, or download {url} (or {url2}) in a browser and have your AI rerun with "
                     "--fd-file <file>.",
        "fd_blocked": "The company-profile download hosts refused our requests for now. To stay polite we will not ask "
                      "them again for 24 hours (retry after {retry_after}). To go on now: download {url} (or {url2}) "
                      "in a browser and have your AI rerun with --fd-file <file>.",
        "tv_blocked": "TradingView refused our requests for now. To stay polite we will not ask it again for 24 hours "
                      "(retry after {retry_after}).",
        "no_credit": "{label} says the account has no credit. Top up at {credits_url}, then tell me 'done'.",
        "key_rejected": "{label} rejected the key. Create a new one and set it again.",
        "key_rejected_head": "{label} rejected the key.",
        "key_rejected_vercel": "Vercel refused this key for Jev (HTTP 403): most likely no AI Gateway credits were bought "
                               "yet (Jev may not be in the free tier), or the key is wrong. Buy some credits at "
                               "{credits_url}, or create a new key and set it again.",
        "store_busy": "Another jev-screen task is using the database. Rerun the same command in a few minutes to "
                      "continue (finished steps are not redone).",
        "queued": "Another idea is being screened in the background; this one is next in line. Your answers are "
                  "saved: check the progress or rerun the same command in a few minutes and it starts.",
        "net_down": "No network; will retry after {t}.",
        "python_old": "This computer's Python is too old. Easiest fix: install uv (one command); it brings its own Python.",
        "failed": "The background work stopped with an error. Rerun the same command to resume; if it happens again, "
                  "show this technical detail to your AI: {error}",
        "fail_duckdb": "The database component duckdb is missing. Have your AI reinstall (pip install -e '.[pdf]'), "
                       "then rerun the same command.",
        "fail_consent": "Your consent to the two data sources is not recorded yet. Answer the consent question, then "
                        "rerun the same command.",
        "fail_canary": "The tiny request that checks the key can pay did not go through (not a credit or key "
                       "problem). Rerun the same command in a few minutes.",
        "fail_results": "The screen finished but its results cannot be read right now (the database may be busy). "
                        "Rerun the same command in a few minutes; nothing is charged twice.",
        "fail_screen_args": "A screen setting is not valid; nothing was sent and nothing was spent. Have your AI read "
                            "the reason in the error field, fix it and rerun.",
        "reused": "This idea was already screened with the same settings: here is that result again (no cost).",
        "interrupted": "The background work stopped: rerun the same command to resume (paid answers are not charged "
                       "again).",
    },
}
NEEDS_AGENT_EN = ("The idea is not in English and no local translation is available. Write one English sentence "
                  "(<= 400 chars, no company names or tickers that are not in the idea) and rerun: {cmd} --idea-en "
                  "'<sentence>'. The human will see it in the spend question.")


# ------------------------------------------------------------------------------------------------ small helpers

def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)


def iso(t: dt.datetime | None) -> str | None:
    return None if t is None else t.replace(microsecond=0).isoformat() + "Z"


def parse_iso(s: Any) -> dt.datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        t = dt.datetime.fromisoformat(s.rstrip("Z"))
    except ValueError:
        return None
    return t.astimezone(dt.timezone.utc).replace(tzinfo=None) if t.tzinfo else t


def idea_key(idea: str) -> str:
    from . import keywords
    return keywords.idea_key(idea)


def sha12(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def detect_lang(idea: str, env: dict[str, str] | None = None) -> str:
    """zh when the idea is Han-dominant without kana / Hangul, or LANG starts with zh; else en."""
    from . import screen
    env = os.environ if env is None else env
    c = screen.script_counts(idea or "")
    han, kana, hangul, latin = c.get("han", 0), c.get("kana", 0), c.get("hangul", 0), c.get("latin", 0)
    if han and not kana and not hangul and han >= latin:
        return "zh"
    if str(env.get("LANG") or "").lower().startswith("zh"):
        return "zh"
    return "en"


def _command(*argv: Any) -> str:
    return " ".join(shlex.quote(str(a)) for a in argv if a is not None)


def _num(v: float) -> str:
    return f"{v:g}" if v < 1e5 else f"{v:.0e}".replace("e+0", "e").replace("e+", "e")


def _cny_words(usd: float) -> str:
    """The yuan figure beside a cost in Chinese text: '约 ¥0.72'; a non-zero cost that rounds to ¥0.00 reads
    '不到 ¥0.01' (never a zero next to a non-zero dollar amount)."""
    y = usd * CNY_PER_USD
    return "不到 ¥0.01" if 0 < y < 0.005 else f"约 ¥{y:.2f}"


def _usd(v: float | None, digits: int = 2) -> str:
    if v is None:
        return "?"
    return f"{v:.{digits}f}" if v >= 0.01 or v == 0 else f"{v:.4f}"


def _abs_jevscreen() -> str:
    from . import agent_cli
    return agent_cli.abs_jevscreen()


# ------------------------------------------------------------------------------------------------ job file

def job_dir(cfg) -> Path:
    return Path(cfg.home) / "quickstart"


def job_path(cfg, key: str) -> Path:
    return job_dir(cfg) / f"{key}.json"


def inbox_path(cfg, key: str) -> Path:
    return job_dir(cfg) / f"{key}.flags.json"


def log_path(cfg, key: str) -> Path:
    return job_dir(cfg) / f"{key}.log"


class JobSecret(RuntimeError):
    """A configured secret would have been written into the job file; the write was refused."""


def load_job(cfg, key: str) -> dict[str, Any] | None:
    try:
        data = json.loads(job_path(cfg, key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("format") == FORMAT else None


def save_job(cfg, job: dict[str, Any]) -> None:
    """Atomic write; refused (JobSecret) when any configured secret appears in it."""
    from . import ops
    text = json.dumps(job, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    hits = ops.scan_secrets(cfg, text)
    if hits:
        raise JobSecret(f"refusing to write the job file: it would contain {', '.join(hits)}")
    path = job_path(cfg, job["idea_key"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_inbox(cfg, key: str) -> dict[str, Any]:
    try:
        data = json.loads(inbox_path(cfg, key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_inbox(cfg, key: str, flags: dict[str, Any]) -> None:
    """A front that found the worker running leaves its flags here (merged over earlier ones)."""
    data = {**read_inbox(cfg, key), **flags, "written_at": iso(now_utc())}
    p = inbox_path(cfg, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def take_inbox(cfg, key: str) -> dict[str, Any]:
    data = read_inbox(cfg, key)
    with contextlib.suppress(OSError):
        inbox_path(cfg, key).unlink()
    data.pop("written_at", None)
    return data


def new_job(idea: str, *, lang: str, min_mcap: float, countries: list[str] | None) -> dict[str, Any]:
    return {"format": FORMAT, "idea": idea, "idea_key": idea_key(idea), "lang": lang, "lang_given": False,
            "idea_en": None, "idea_en_source": None, "idea_en_agent": False, "min_mcap_usd": min_mcap,
            "countries": countries, "sieve_version": None, "fd_file": None, "no_open": False,
            "state": "new", "worker": {"pid": None, "started_at": None, "heartbeat_at": None},
            "steps": [], "progress": {"phase": 0, "done": None, "total": None, "text_zh": None, "text_en": None,
                                      "heartbeat_at": None},
            "failures": {}, "failure": None, "waiting_on": None, "approval": None, "approval_agent": False,
            "canary": None, "estimate": None, "reprice": None, "opened_run_ids": [], "result": None,
            "notes": [], "created_at": iso(now_utc())}


def step_of(job: dict[str, Any], sid: str) -> dict[str, Any] | None:
    return next((s for s in job.get("steps") or [] if s.get("id") == sid), None)


def step_done(job: dict[str, Any], sid: str) -> bool:
    s = step_of(job, sid)
    return bool(s) and s.get("status") in ("ok", "skipped")


def reset_steps(job: dict[str, Any], *ids: str) -> None:
    job["steps"] = [s for s in job.get("steps") or [] if s.get("id") not in ids]


# ------------------------------------------------------------------------------------------------ decisions

def consent_state(cfg) -> dict[str, Any]:
    from . import consent
    return consent.get(cfg, consent.GRAY_SOURCES)


def consent_current(c: dict[str, Any]) -> bool:
    """A 'yes' to the current consent statement (an older statement's 'yes' is asked once more)."""
    from . import consent
    return c["state"] == "yes" and (c.get("statement_version") or 1) >= consent.STATEMENT_VERSION


def key_state(cfg) -> dict[str, Any]:
    """{'configured', 'fingerprint', 'provider', 'reason'} of the active Jev provider (jev.resolve_provider):
    presence by stat only; the fingerprint is provider + file mtime + size (never the value), so a new key or a
    switch of provider is tested again. reason 'default' = no Jev key at all (the account question is asked)."""
    from . import jev, keys
    try:
        prov, reason = jev.resolve_provider(cfg)
    except jev.ProviderError as e:
        return {"configured": False, "fingerprint": None, "provider": None, "reason": "error", "error": str(e)}
    p = keys.presence(cfg, prov.name)
    fp = None
    if p.get("configured"):
        if p.get("source") == "env":
            fp = "env"
        else:
            with contextlib.suppress(OSError):
                st = Path(p["path"]).stat()
                fp = f"{st.st_mtime_ns}:{st.st_size}"
        if fp is not None and prov.name != "openrouter":     # OpenRouter keeps its old fingerprints valid
            fp = f"{prov.name}:{fp}"
    return {"configured": bool(p.get("configured")), "fingerprint": fp, "provider": prov.name, "reason": reason}


def _provider(name: str | None):
    from . import jev
    try:
        return jev.provider_named(name) if name else jev.PROVIDERS[jev.DEFAULT_PROVIDER]
    except jev.ProviderError:
        return jev.PROVIDERS[jev.DEFAULT_PROVIDER]


def account_question(lang: str) -> str:
    """The one plain question asked when no Jev key is configured: which account the human has."""
    from . import jev
    P = jev.PROVIDERS
    return STRINGS["zh" if lang == "zh" else "en"]["account_q"].format(
        ts_signup=P["typesafe"].signup_url, ts_keys=P["typesafe"].key_url, or_signup=P["openrouter"].signup_url,
        or_keys=P["openrouter"].key_url, vc_signup=P["vercel"].signup_url, vc_keys=P["vercel"].key_url)


def zh_tidy(text: str) -> str:
    """Chinese text built from templates: no half-width space between two Chinese characters or after a full-width
    stop ('TypeSafe 官方 说' -> 'TypeSafe 官方说'); the space between a Latin word and a Chinese one stays."""
    return _ZH_SPACE.sub("", text)


_ZH_SPACE = re.compile(r"(?<=[\u4e00-\u9fff。，；：！？）」』]) +(?=[\u4e00-\u9fff（「『])")


def provider_label(pr, lang: str) -> str:
    """A provider's name in the human's language (TypeSafe 官方 in Chinese text, never English words there)."""
    return pr.label_zh if lang == "zh" else pr.label


def key_item(cfg, k: dict[str, Any], rejected: bool, http_status: Any = None) -> dict[str, Any]:
    """The pending Jev key item. No key at all: the account question with one choice per provider (the agent runs
    the chosen one's agent_try once the human names it and says the key is ready). A known provider (its key was
    rejected, or the human chose it and its key is missing): that provider's key step, plus `choices` for another
    account (setting another provider's key switches to it; `clear_command` forgets this one) - except when
    JEVSCREEN_JEV_PROVIDER pins the provider."""
    from . import agent_cli, jev
    item: dict[str, Any] = {"id": "key_jev", "human_action": True, "rejected": rejected}
    choices = []
    for n in jev.PROVIDER_ORDER:
        pr = jev.PROVIDERS[n]
        choices.append({"provider": n, "label": pr.label, "label_zh": pr.label_zh, "signup_url": pr.signup_url,
                        "key_url": pr.key_url, "agent_try": f"jevscreen keys set {n} --dialog",
                        "agent_try_file": f"jevscreen keys set {n} --from-file <the file the human saved it in>",
                        "human_command": f"{_abs_jevscreen()} keys set {n}"})

    def cmds(lang: str, skip: str | None = None) -> str:
        sep = "；" if lang == "zh" else "; "
        colon = "：" if lang == "zh" else ": "
        return sep.join(f"{c['label_zh' if lang == 'zh' else 'label']}{colon}`{c['human_command']}`"
                        for c in choices if c["provider"] != skip)

    if k.get("reason") in ("default", "error") and not rejected:
        for lang in ("zh", "en"):
            q = account_question(lang)
            item[f"question_{lang}"] = q
            item[f"text_{lang}"] = q + (" " if lang == "en" else "") + STRINGS[lang]["account_fallback"].format(
                terminal=agent_cli.terminal_hint(lang), cmds=cmds(lang))
        item.update(provider=None, choices=choices)
        if k.get("error"):
            item["provider_error"] = k["error"]
        return item
    pr = _provider(k.get("provider"))
    absc = f"{_abs_jevscreen()} keys set {pr.name}"
    pinned = k.get("reason") == "explicit"          # JEVSCREEN_JEV_PROVIDER: another key would not switch
    item.update(provider=pr.name, label=pr.label, label_zh=pr.label_zh,
                agent_try=f"jevscreen keys set {pr.name} --dialog", agent_try_file=f"jevscreen keys set {pr.name} --from-file <the file the human saved it in>",
                human_command=absc)
    if not pinned:
        item.update(choices=[c for c in choices if c["provider"] != pr.name],
                    clear_command=f"jevscreen keys clear {pr.name}")
    for lang in ("zh", "en"):
        T = STRINGS[lang]
        label = provider_label(pr, lang)
        head, sp = "", (" " if lang == "en" else "")
        vercel_403 = pr.name == "vercel" and http_status == 403
        if rejected:
            key = "key_rejected_vercel" if vercel_403 else "key_rejected_head"
            head = T[key].format(label=label, credits_url=pr.credits_url) + sp
        hint = (T["key_limit"] if pr.name == "openrouter" else
                T["key_credits"].format(credits_url=pr.credits_url) if pr.name == "vercel" and not vercel_403 else "")
        text = head + T["key"].format(
            label=label, key_url=pr.key_url, limit=hint,
            terminal=agent_cli.terminal_hint(lang), abs_cmd=absc) + (
            "" if pinned else sp + T["key_other"].format(cmds=cmds(lang, pr.name)))
        item[f"text_{lang}"] = zh_tidy(text) if lang == "zh" else text
    return item


def _sieve(cfg, idea: str) -> dict[str, Any] | None:
    from . import calib
    try:
        return calib.load_sieve(calib.sieve_path(cfg, idea))
    except ValueError:
        return None


def _cached_model_en(cfg, idea: str) -> str | None:
    """idea_en from the local model's cache (never loads the model here: the front must stay fast)."""
    from . import keywords
    try:
        data = keywords._read_cache(keywords.cache_path(cfg, idea), idea)
    except Exception:  # noqa: BLE001
        return None
    en = (data or {}).get("idea_en") if isinstance(data, dict) else None
    return en.strip() if isinstance(en, str) and en.strip() else None


def resolve_idea_en(cfg, job: dict[str, Any]) -> None:
    """Pin idea_en when it is not pinned yet: sieve > the idea itself (English ideas only) > local-model cache.
    A sentence the worker refused (it names a company, see Worker.check_idea_en) is never pinned again."""
    from . import screen
    if job.get("idea_en"):
        return
    idea = job["idea"]
    refused = set(job.get("idea_en_refused") or [])
    sv = _sieve(cfg, idea)
    sv_en = sv["idea_en"].strip() if isinstance((sv or {}).get("idea_en"), str) else ""
    if sv_en and sv_en not in refused:
        job.update(idea_en=sv_en, idea_en_source="sieve")
    elif not screen.needs_translation(idea):
        job.update(idea_en=idea.strip(), idea_en_source="verbatim")
    else:
        en = _cached_model_en(cfg, idea)
        if en and en not in refused:
            job.update(idea_en=en, idea_en_source="model")


def sieve_drift(cfg, job: dict[str, Any], flags: dict[str, Any]) -> None:
    """Precedence of the English sentence (spec 6.2): --idea-en > sieve.idea_en > the job's pin. A sieve idea_en
    that differs from the pin (the sieve written after the first result) replaces a pin nothing was approved or
    screened with, and otherwise becomes the reprice question (a new English voids the human's approval)."""
    if flags.get("idea_en") or not job.get("idea_en"):
        return
    sv = _sieve(cfg, job["idea"])
    sv_en = sv["idea_en"].strip() if isinstance((sv or {}).get("idea_en"), str) else ""
    if not sv_en or sv_en == job["idea_en"] or sv_en in (job.get("idea_en_refused") or []) \
            or sv_en in (job.get("idea_en_declined") or []):
        return
    if (job.get("reprice") or {}).get("new") == sv_en:
        return
    if job.get("approval") or step_done(job, "screen"):
        job["reprice"] = {"old": job["idea_en"], "new": sv_en, "source": "sieve"}
    else:
        job.update(idea_en=sv_en, idea_en_source="sieve", idea_en_agent=False, reprice=None, estimate=None)
        reset_steps(job, "idea_en", "estimate", "screen", "fetch", "finish")


def names_cache_path(cfg) -> Path:
    """<home>/quickstart/names.json: the universe's company names and tickers, written by the worker right after
    the stock list is in, so the front can check an --idea-en without the database (locked by a running worker)."""
    return job_dir(cfg) / "names.json"


def write_names_cache(cfg, names: list[str], tickers: list[str]) -> None:
    p = names_cache_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"written_at": iso(now_utc()), "names": names, "tickers": tickers},
                              ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def _universe_names(cfg) -> tuple[list[str], list[str], str]:
    """(names, tickers, source) for the front's --idea-en check: the store (read-only, never waits on a writer),
    else the names cache the worker wrote after the last stock-list download, else nothing ('none': the sentence
    passes now and is checked once, right after the first stock list is in)."""
    from . import calib, store
    if Path(cfg.db_path).exists():
        try:
            with store.session(cfg, read_only=True, wait_s=VIEW_DB_WAIT_S, poll_s=VIEW_DB_POLL_S) as con:
                names, tickers = calib.load_names(con)
            if names or tickers:
                return names, tickers, "store"
        except Exception:  # noqa: BLE001 - locked by the worker: the cache below
            pass
    try:
        data = json.loads(names_cache_path(cfg).read_text(encoding="utf-8"))
        names, tickers = list(data.get("names") or []), list(data.get("tickers") or [])
        if names or tickers:
            return names, tickers, "cache"
    except (OSError, ValueError, AttributeError):
        pass
    return [], [], "none"


def _held_approval(job: dict[str, Any]) -> dict[str, Any] | None:
    """The refused sentence an approval was given for ({'text', 'hits', 'problems'}), when the worker refused the
    English the human had approved (it looked like it named a company once the stock list was in): the approval is
    held for a fix of that sentence instead of being dropped."""
    last = job.get("idea_en_held") or {}
    a = job.get("approval") or {}
    if last.get("text") and a and a.get("idea_en_sha") == sha12(last["text"]):
        return last
    return None


def approval_valid(job: dict[str, Any]) -> bool:
    a = job.get("approval")
    return bool(a) and a.get("idea_en_sha") == sha12(job.get("idea_en"))


def apply_flags(cfg, job: dict[str, Any], flags: dict[str, Any]) -> list[str]:
    """Apply the agent's flags to the job (lock holder only). Returns problems for the agent (idea_en refused)."""
    from . import calib
    problems: list[str] = []
    if flags.get("retry"):
        job["failures"] = {}
        if job.get("failure") and job["failure"].get("kind") in ("network", "failed", "busy", "fd"):
            job["failure"] = None
    if flags.get("fd_file"):
        job["fd_file"] = str(flags["fd_file"])
        job["failures"].pop("descriptions", None)
        reset_steps(job, "descriptions")
        f = job.get("failure") or {}
        if f.get("kind") == "fd" or (f.get("kind") == "blocked" and f.get("source") == "fd"):
            job["failure"] = None          # a manual download needs no request: no cooldown applies
    if flags.get("no_open"):
        job["no_open"] = True
    if flags.get("lang") in ("zh", "en"):
        job.update(lang=flags["lang"], lang_given=True)
    for k in ("min_mcap_usd", "countries"):
        if k in flags and flags[k] is not None and flags[k] != job.get(k):
            job[k] = flags[k]
            reset_steps(job, "estimate", "screen", "fetch", "finish")
            job["estimate"] = None
            if job.get("waiting_on") == "approve_budget" and job.get("waiting_kind") in (None, "over"):
                job.update(waiting_on=None, waiting_kind=None)    # a narrower scope: estimated again first
    new_en = flags.get("idea_en")
    if isinstance(new_en, str) and new_en.strip():
        new_en = " ".join(new_en.split()) if "\n" not in new_en else new_en.strip()
        names, tickers, _src = _universe_names(cfg)
        probs = calib.idea_en_problems(new_en, job["idea"], names, tickers)
        held = _held_approval(job)
        if probs:
            problems += probs
            hits = calib.idea_en_name_hits(new_en, job["idea"], names, tickers)
            job["idea_en_last_refused"] = {"text": new_en, "hits": hits, "problems": probs,
                                           "suggested": calib.suggest_idea_en(new_en, hits), "at": iso(now_utc()),
                                           "by": "front"}
            if not job.get("idea_en"):
                job["idea_en_problems"] = probs
        elif held is not None and not job.get("idea_en"):
            # the sentence the human approved was refused (it looked like it named a company): a fix of only the
            # flagged word keeps that approval; any other change is a new English sentence (the reprice rule)
            a = job["approval"]
            r = job.get("reprice") or {}
            if calib.idea_en_minor_fix(held["text"], new_en, held.get("hits") or []):
                a["idea_en_sha"] = sha12(new_en)
                job.update(idea_en=new_en, idea_en_source="agent", idea_en_agent=True, reprice=None,
                           idea_en_problems=None, idea_en_last_refused=None, idea_en_held=None, estimate=None)
                job["idea_en_changes"] = (job.get("idea_en_changes") or []) + [
                    {"old": held["text"], "new": new_en, "words": held.get("hits") or [], "kind": "false_positive_fix",
                     "at": iso(now_utc())}]
                job["notes"] = [n for n in job.get("notes") or [] if not str(n).startswith("idea_en refused")] + [
                    f"idea_en: {held['text']!r} -> {new_en!r} (only the word mistaken for a company name changed; "
                    "the budget approval stands)"]
                reset_steps(job, "idea_en", "estimate", "screen", "fetch", "finish")
            elif r.get("old_refused") and r.get("new") == new_en and flags.get("approve_budget") is not None:
                a.update(usd=float(flags["approve_budget"]), idea_en_sha=sha12(new_en), approved_at=iso(now_utc()))
                job.update(idea_en=new_en, idea_en_source="agent", idea_en_agent=True, reprice=None,
                           idea_en_problems=None, idea_en_last_refused=None, idea_en_held=None, estimate=None,
                           approval_agent=True)
                job["notes"] = [n for n in job.get("notes") or [] if not str(n).startswith("idea_en refused")]
                reset_steps(job, "idea_en", "estimate", "screen", "fetch", "finish")
            else:
                job["reprice"] = {"old": held["text"], "new": new_en, "old_refused": True,
                                  "old_hits": held.get("hits") or [], "old_problems": held.get("problems") or []}
        elif new_en != job.get("idea_en"):
            pinned = job.get("idea_en") and job.get("idea_en_source") in ("agent", "sieve", "model") \
                and (job.get("approval") or step_done(job, "screen"))
            if pinned and flags.get("approve_budget") is None:
                job["reprice"] = {"old": job["idea_en"], "new": new_en}
            else:
                job.update(idea_en=new_en, idea_en_source="agent", idea_en_agent=True, reprice=None,
                           idea_en_problems=None, idea_en_last_refused=None, idea_en_held=None,
                           idea_en_declined=[x for x in job.get("idea_en_declined") or [] if x != new_en])
                if job.get("approval") and job["approval"].get("idea_en_sha") != sha12(new_en):
                    job["approval"] = None          # the human approved the English they saw
                reset_steps(job, "idea_en", "estimate", "screen", "fetch", "finish")
                job["estimate"] = None
        elif job.get("reprice"):
            # the pinned sentence again while a new one waits for the human: a no (the sieve's sentence is not
            # asked again until it changes)
            job["idea_en_declined"] = list(dict.fromkeys((job.get("idea_en_declined") or [])
                                                         + [job["reprice"]["new"]]))
            job["reprice"] = None
    x = flags.get("approve_budget")
    if x is not None and job.get("idea_en") and not job.get("reprice"):
        x = float(x)
        a = job.get("approval")
        raised = False
        if a and a.get("idea_en_sha") == sha12(job["idea_en"]):
            raised = x > float(a["usd"]) + 1e-9
            a.update(usd=x, approved_at=iso(now_utc()))
        else:
            job["approval"] = {"usd": x, "idea_en_sha": sha12(job["idea_en"]), "approved_at": iso(now_utc()),
                               "out_dirs": [], "canary_usd": 0.0}
        job["approval_agent"] = True
        if job.get("waiting_on") == "approve_budget":
            job["waiting_on"] = None
        reset_steps(job, "estimate") if not step_done(job, "screen") else None
        if raised and exhausted(job):
            # a top-up after the budget ran out: screen again under the larger total (paid answers come from the
            # Jev cache for $0, so the new run continues where the old one stopped)
            reset_steps(job, "estimate", "screen", "fetch", "finish")
            job.update(state="new", result=None, waiting_on=None)
    fd = flags.get("fill_descriptions")
    if fd in ("yes", "no") and job.get("fill_offer") and not (job.get("fill") or {}).get("answer"):
        job["fill"] = {"answer": fd, "at": iso(now_utc()), "offer": job["fill_offer"]}
    if flags.get("new_run") and job.get("state") in ("done", "failed"):
        reset_steps(job, "estimate", "screen", "fetch", "finish")
        job.update(result=None, state="new", failure=None)
    return problems


def read_ledger(cfg, dirs: list[str], *, wait_s: float, poll_s: float = 0.5) -> float:
    """Jev cost in the ledger of the screen runs written to `dirs` (raises store.StoreLocked)."""
    from . import store
    with store.session(cfg, read_only=True, wait_s=wait_s, poll_s=poll_s) as con:
        row = con.execute(
            "SELECT coalesce(sum(r.cost_usd), 0) FROM jev_requests r WHERE r.run_id IN "
            "(SELECT run_id FROM screen_runs WHERE list_contains(?::VARCHAR[], output_dir))", [dirs]).fetchone()
    return float(row[0] or 0.0)


def spent_usd(cfg, job: dict[str, Any], *, strict: bool = False, wait_s: float | None = None) -> float:
    """Jev cost of the screens run under the approval (ledger, by the runs' output directories) + the canary.

    The views (front, status) wait at most VIEW_DB_WAIT_S for the store: when another process writes, the worker's
    last ledger reading (approval.ledger_usd) stands in. The worker (strict=True) waits WORKER_DB_WAIT_S and raises
    store.StoreLocked rather than compute a screen budget from partial numbers."""
    a = job.get("approval") or {}
    total = float(a.get("canary_usd") or 0.0)
    dirs = [d for d in a.get("out_dirs") or [] if d]
    recorded = sum(float(x or 0.0) for x in (a.get("costs") or {}).values())
    ledger = float(a.get("ledger_usd") or 0.0)
    if dirs and Path(cfg.db_path).exists():
        if strict:
            ledger = read_ledger(cfg, dirs, wait_s=WORKER_DB_WAIT_S if wait_s is None else wait_s)
            a["ledger_usd"] = round(ledger, 8)
        else:
            try:
                ledger = max(ledger, read_ledger(cfg, dirs, wait_s=VIEW_DB_WAIT_S if wait_s is None else wait_s,
                                                 poll_s=VIEW_DB_POLL_S))
            except Exception:  # noqa: BLE001 - locked: the worker's last reading stands in (views only)
                pass
    # the ledger also holds a run killed before it returned; the recorded costs cover a run the ledger lags
    return round(total + max(ledger, recorded), 6)


def remaining_usd(cfg, job: dict[str, Any], *, spent: float | None = None, strict: bool = False) -> float | None:
    a = job.get("approval")
    if not a:
        return None
    s = spent_usd(cfg, job, strict=strict) if spent is None else spent
    return round(max(0.0, float(a["usd"]) - s), 6)


def pending(cfg, job: dict[str, Any], spent: float | None = None) -> list[dict[str, Any]]:
    """Every open item, the agent's first (idea_en), then the human's (consent, budget, key). `spent`: the
    approval's spend when the caller already read it (one ledger read per response)."""
    from . import doctor
    items: list[dict[str, Any]] = []
    if exhausted(job) and approval_valid(job):
        return [budget_item(cfg, job, spent, kind="topup")]
    if job.get("state") == "done" and job.get("reprice"):
        return [reprice_item(job)]            # a new English sentence after the result: asked, never stored silently
    if job.get("state") == "done":
        f = fill_item(cfg, job, spent)
        return [f] if f is not None else items
    if job.get("state") == "declined":
        return items
    lang = job.get("lang") or "zh"
    base = front_command(job)
    if not job.get("idea_en") and not (job.get("reprice") or {}).get("old_refused"):
        refused = job.get("idea_en_problems") or []
        last = job.get("idea_en_last_refused") or {}
        sug = last.get("suggested") if refused else None
        item = {"id": "idea_en", "ask_agent": True, "instructions_en": NEEDS_AGENT_EN.format(cmd=base)
                + (f" The last sentence was refused: {'; '.join(refused)}." if refused else ""),
                "rerun_with": "--idea-en '<sentence>'", "problems": refused,
                "refused_idea_en": last.get("text") if refused else None,
                "suggested_idea_en": sug,
                "rerun_command": front_command(job, idea_en=sug or "<sentence>")}
        if refused and _held_approval(job) is not None:
            item["instructions_en"] += (" The human already approved the budget for the refused sentence: if the "
                                        "flagged word is an ordinary English word and you only lower-case it (e.g. "
                                        "'Harmonic' -> 'harmonic') or swap it for Core / Main / Key / Leading / "
                                        "Major / Primary, that approval stands and nobody is asked again; any other "
                                        "change (a company name dropped or replaced, other words) is asked again "
                                        "(reprice_idea_en). rerun_command keeps --approve-budget.")
        items.append(item)
    c = consent_state(cfg)
    if not consent_current(c):
        if c["state"] != "no":
            from . import consent as _consent
            items.append({"id": "consent_gray_sources", "ask_human": True, "question_zh": doctor.GRAY_QUESTION_ZH,
                          "question_en": doctor.GRAY_QUESTION, "statement_version": doctor.GRAY_STATEMENT_VERSION,
                          "note_zh": doctor.recipient_note(cfg, "zh"), "note_en": doctor.recipient_note(cfg, "en"),
                          "answer_words": _consent.ANSWER_WORDS,
                          "record_answer_commands": [f"jevscreen consent set gray-sources {v} --lang {lang}"
                                                     for v in ("yes", "no")]})
    if job.get("reprice"):
        items.append(reprice_item(job))
    if job.get("idea_en"):
        b = budget_item(cfg, job, spent)
        if b is not None:
            items.append(b)
    k = key_state(cfg)
    canary = job.get("canary") or {}
    rejected = canary.get("status") in (401, 403) and canary.get("fingerprint") == k["fingerprint"]
    if not k["configured"] or rejected:
        items.append(key_item(cfg, k, rejected, canary.get("status")))
    return items


def reprice_item(job: dict[str, Any]) -> dict[str, Any]:
    """The question for a new English sentence after an approval or a screen: yes = rerun with `rerun_with` (the new
    sentence and the cap again); no = rerun with `decline_with` (the pinned sentence), which drops the question."""
    r = job["reprice"]
    l1 = (job.get("estimate") or {}).get("l1_usd") or 0.24
    x = float((job.get("approval") or {}).get("usd") or DEFAULT_APPROVAL_USD)
    if r.get("old_refused"):
        # the approved sentence was refused (it looked like it named a company) and the new one says something
        # else: the human confirms the new English; a no means the agent writes another sentence
        words = ", ".join(r.get("old_hits") or []) or "?"
        why_zh, why_en = f"它把 {words} 当成了公司名", f"it names {words}"
        return {"id": "reprice_idea_en", "ask_human": True, "old_idea_en": r["old"], "new_idea_en": r["new"],
                "old_refused": True,
                "question_zh": STRINGS["zh"]["idea_en_refused_q"].format(old=r["old"], new=r["new"], why=why_zh,
                                                                          x=f"{x:g}"),
                "question_en": STRINGS["en"]["idea_en_refused_q"].format(old=r["old"], new=r["new"], why=why_en,
                                                                          x=f"{x:g}"),
                "approve_usd": x, "rerun_with": f"--idea-en {shlex.quote(r['new'])} --approve-budget {x:g}",
                "decline_with": None,
                "on_no_en": "Write another English sentence (change only the flagged word to keep the meaning) and "
                            "rerun next_command with --idea-en '<sentence>'."}
    return {"id": "reprice_idea_en", "ask_human": True, "old_idea_en": r["old"], "new_idea_en": r["new"],
            "question_zh": STRINGS["zh"]["reprice_q"].format(old=r["old"], new=r["new"], l1=_usd(l1), x=f"{x:g}"),
            "question_en": STRINGS["en"]["reprice_q"].format(old=r["old"], new=r["new"], l1=_usd(l1), x=f"{x:g}"),
            "approve_usd": x, "rerun_with": f"--idea-en {shlex.quote(r['new'])} --approve-budget {x:g}",
            "decline_with": f"--idea-en {shlex.quote(r['old'])}"}


def fill_market(job: dict[str, Any]) -> str | None:
    """The market a profile fill is offered for: the one country the idea was narrowed to, else China for a Chinese
    idea (Han script without kana / Hangul); None for any other idea (no question)."""
    from . import screen
    cs = job.get("countries") or []
    if len(cs) == 1 and str(cs[0]).upper() in FILL_MARKET_WORDS:
        return str(cs[0]).upper()
    if cs:
        return None
    c = screen.script_counts(job.get("idea") or "")
    return "CN" if c.get("han") and not c.get("kana") and not c.get("hangul") else None


_SHARE_CLASS = re.compile(r"[\s,]+(?:Class\s+[A-Z]|[A-Z]\s+Shares?|\(?[A-Z]-?Shares?\)?)$", re.IGNORECASE)


def plain_name(name: str | None) -> str:
    """A company's English name as a person says it: no share class ('Class A') and no 'Co., Ltd.'."""
    from . import calib
    n = (name or "").strip()
    prev = None
    while prev != n:
        prev, n = n, _SHARE_CLASS.sub("", n).strip(" ,")
    return calib._short_name(n) or n


def _local_names(con, security_ids: list[str]) -> dict[str, str]:
    """security_id -> the exchange short name (CNINFO 证券简称, no ST mark) from the newest stored CNINFO stock list;
    empty when there is none."""
    from . import shells
    try:
        got = shells._newest_list(con)
        if got is None or not security_ids:
            return {}
        by_org = {r["org_id"]: shells.strip_st(r.get("name")) for r in shells.read_stock_list(got[0]) if r.get("name")}
        rows = con.execute("SELECT security_id, id_value FROM identifiers WHERE id_type = ? AND "
                           "list_contains(?::VARCHAR[], security_id)", [shells.ST_ID_TYPE, security_ids]).fetchall()
    except Exception:  # noqa: BLE001 - the English name stands in
        return {}
    return {sid: by_org[org] for sid, org in rows if by_org.get(org)}


def fill_examples(con, run_id: str | None, missing: list[tuple]) -> tuple[list[dict[str, Any]], str]:
    """The missing companies the fill question names (at most FILL_NAMES) and why ('related' | 'largest'): first
    those in the industries of the run's L1 passes (most passes first, then market cap), never a bank or insurer
    unless the passes include finance; else the largest non-financial ones. `missing` rows: (security_id, name,
    country, exchange, market_cap_usd, described, sector, industry), largest first."""
    from collections import Counter
    from . import screen
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
    basis, picked = ("related", related[:FILL_NAMES]) if related else ("largest", pool[:FILL_NAMES])
    local = _local_names(con, [r[0] for r in picked])
    return [{"name": r[1], "name_en": plain_name(r[1]), "name_zh": local.get(r[0]) or plain_name(r[1]),
             "ticker": r[0].split(":", 1)[-1], "security_id": r[0], "market_cap_usd": r[4],
             "industry": r[7]} for r in picked], basis


def fill_item(cfg, job: dict[str, Any], spent: float | None = None) -> dict[str, Any] | None:
    """The one optional question after a result whose market has a large profile hole (Worker.fill_offer): asked
    once; yes = rerun next_command + rerun_with (the fill runs in the background, the result stays meanwhile),
    no = decline_with (never asked again)."""
    o = job.get("fill_offer")
    if not o or (job.get("fill") or {}).get("answer") or job.get("reprice"):
        return None
    a = job.get("approval") or {}
    x = float(a.get("usd") or DEFAULT_APPROVAL_USD)
    rem = remaining_usd(cfg, job, spent=spent)
    rem = x if rem is None else rem
    mz, me = FILL_MARKET_WORDS.get(o["country"], (o["country"], o["country"]))
    big = o.get("biggest") or []
    nz = "、".join(f"{b.get('name_zh') or b['name']}（{b['ticker']}，${float(b['market_cap_usd'] or 0) / 1e9:.1f}B）"
                  for b in big)
    ne = ", ".join(f"{b.get('name_en') or b['name']} ({b['ticker']}, ${float(b['market_cap_usd'] or 0) / 1e9:.1f}B)"
                   for b in big)
    basis = "related" if o.get("names_basis") == "related" else "largest"
    nz = STRINGS["zh"][f"fill_names_{basis}"].format(names=nz) if big else ""
    ne = STRINGS["en"][f"fill_names_{basis}"].format(names=ne) if big else ""
    floor = f"${float(o.get('floor_usd') or job.get('min_mcap_usd') or 0) / 1e9:g}B"
    kw = dict(n=o["missing"], pct=round(100 * float(o.get("share") or 0)), m=o["minutes"],
              est=_usd(o["estimate_usd"]), x=f"{x:g}", rem=_usd(rem), floor=floor)
    return {"id": "fill_descriptions", "ask_human": True, "optional": True, "country": o["country"],
            "missing": o["missing"], "companies": o.get("companies"), "share": o.get("share"),
            "biggest": big, "minutes": o["minutes"], "estimate_usd": o["estimate_usd"],
            "question_zh": STRINGS["zh"]["fill_q"].format(market=mz, names=nz, **kw),
            "question_en": STRINGS["en"]["fill_q"].format(market=me, names=ne, **kw),
            "rerun_with": "--fill-descriptions yes", "decline_with": "--fill-descriptions no"}


def fill_waiting(job: dict[str, Any]) -> bool:
    """A finished job whose human said yes to the profile fill that has not run yet."""
    fill = job.get("fill") or {}
    return job.get("state") == "done" and fill.get("answer") == "yes" and not fill.get("finished_at")


def exhausted(job: dict[str, Any]) -> bool:
    """A finished job whose screen ran out of budget (the page shows what was checked; a top-up continues)."""
    return job.get("state") == "done" and (job.get("result") or {}).get("status") == "budget_exhausted"


def budget_item(cfg, job: dict[str, Any], spent: float | None = None, *,
                kind: str | None = None) -> dict[str, Any] | None:
    """The approve_budget item when one is open: no approval for this idea_en, or the worker waits on the budget
    (estimate over the approval, a top-up after the budget ran out, uncertain items over 1 cent)."""
    en = job["idea_en"]
    est = job.get("estimate") or {}
    if not approval_valid(job):
        x = DEFAULT_APPROVAL_USD
        e = est.get("est_cost_usd")
        est_zh = _usd(e) if e is not None else f"{EXPECTED_RANGE[0]}–{EXPECTED_RANGE[1]}"
        return {"id": "approve_budget", "ask_human": True, "kind": "first", "idea_en": en,
                "question_zh": STRINGS["zh"]["budget_q"].format(idea_en=en, x=f"{x:g}", y=f"{x * CNY_PER_USD:.0f}",
                                                                est=est_zh),
                "question_en": STRINGS["en"]["budget_q"].format(idea_en=en, x=f"{x:g}", est=est_zh),
                "estimate_usd": e, "reserved_usd": est.get("est_reserved_usd"), "remaining_usd": None,
                "rerun_with": f"--approve-budget {x:g}", "alternatives": []}
    if kind is None and job.get("waiting_on") != "approve_budget":
        return None
    a = job["approval"]
    spent = spent_usd(cfg, job) if spent is None else spent
    rem = remaining_usd(cfg, job, spent=spent) or 0.0
    kind = kind or job.get("waiting_kind") or "over"
    if kind == "over":
        r = float(est.get("est_reserved_usd") or 0.0)
        r_up = math.ceil(max(r + spent, a["usd"]) * 10) / 10 + 0.1
        alts = est.get("alternatives") or []
        az = "、".join(f"{x['label_zh']}（约 ${_usd(x['estimate_usd'])}）" for x in alts) or "（没有更小的范围）"
        ae = ", ".join(f"{x['label_en']} (about ${_usd(x['estimate_usd'])})" for x in alts) or "(no narrower scope)"
        qz = STRINGS["zh"]["over_q"].format(r=_usd(r), x=f"{a['usd']:g}", alts=az, r_up=f"{r_up:.2f}")
        qe = STRINGS["en"]["over_q"].format(r=_usd(r), x=f"{a['usd']:g}", alts=ae, r_up=f"{r_up:.2f}")
        rerun = f"--approve-budget {r_up:.2f}"
    elif kind == "uncertain":
        c = float(job.get("uncertain_usd") or 0.0)
        total = math.ceil((a["usd"] - rem + c + 0.01) * 100) / 100
        n = int(job.get("uncertain_items") or 0)
        qz = STRINGS["zh"]["uncertain_q"].format(n=n, c=_usd(c), total=f"{total:.2f}")
        qe = STRINGS["en"]["uncertain_q"].format(n=n, c=_usd(c), total=f"{total:.2f}")
        rerun, r, alts = f"--approve-budget {total:.2f}", c, []
    else:   # topup
        more = float(job.get("topup_usd") or 0.25)
        total = math.ceil((a["usd"] + more) * 100) / 100
        qz = STRINGS["zh"]["budget_topup_q"].format(more=_usd(more), total=f"{total:.2f}")
        qe = STRINGS["en"]["budget_topup_q"].format(more=_usd(more), total=f"{total:.2f}")
        rerun, r, alts = f"--approve-budget {total:.2f}", more, []
    return {"id": "approve_budget", "ask_human": True, "kind": kind, "idea_en": en, "question_zh": qz,
            "question_en": qe, "estimate_usd": est.get("est_cost_usd"), "reserved_usd": r, "remaining_usd": rem,
            "rerun_with": rerun, "alternatives": alts}


# ------------------------------------------------------------------------------------------------ commands

def front_command(job: dict[str, Any], *extra: Any, idea_en: str | None = None) -> str:
    """The front command for this job with the flags the agent already passed (never consent, and
    --approve-budget only when the agent passed it: also while that approval is held for a fix of a refused
    English sentence). idea_en: the sentence to put in (a refused one's fix) instead of the pinned one."""
    argv: list[Any] = ["jevscreen", "quickstart", job["idea"]]
    if job.get("min_mcap_usd") not in (None, DEFAULT_MIN_MCAP):
        argv += ["--min-mcap", _num(float(job["min_mcap_usd"]))]
    if job.get("countries"):
        argv += ["--countries", ",".join(job["countries"])]
    if job.get("lang_given"):
        argv += ["--lang", job["lang"]]
    if idea_en is not None:
        argv += ["--idea-en", idea_en]
    elif job.get("idea_en_agent") and job.get("idea_en"):
        argv += ["--idea-en", job["idea_en"]]
    if job.get("approval_agent") and job.get("approval"):
        argv += ["--approve-budget", f"{float(job['approval']['usd']):g}"]
    return _command(*argv, *extra)


def status_command(job: dict[str, Any], wait: bool = True) -> str:
    return _command("jevscreen", "quickstart", "--status", "--key", job["idea_key"],
                    *(("--wait", str(int(WAIT_DEFAULT_S))) if wait else ()), "--json")


# ------------------------------------------------------------------------------------------------ response

def _progress_text(job: dict[str, Any], lang: str) -> str | None:
    p = job.get("progress") or {}
    return p.get(f"text_{lang}")


def derive_status(cfg, job: dict[str, Any], items: list[dict[str, Any]]) -> str:
    st = job.get("state")
    if st == "declined":
        return "declined"
    if st == "done" and any(i.get("id") == "reprice_idea_en" for i in items):
        return "needs_human"
    if st == "done" and job.get("queued") and fill_waiting(job):
        return "store_busy"      # the yes to the profile fill waits for another idea's worker
    if st == "done":
        r = (job.get("result") or {}).get("status")
        return {"ok": "done", "partial": "partial", "budget_exhausted": "budget_exhausted"}.get(r, "done")
    if any(i.get("ask_agent") for i in items):
        return "needs_agent"
    if st == "failed":
        kind = (job.get("failure") or {}).get("kind")
        return {"blocked": "blocked", "busy": "store_busy", "ai_unavailable": "ai_unavailable"}.get(kind, "failed")
    if any(i.get("ask_human") or i.get("human_action") for i in items):
        return "needs_human"
    if st == "interrupted":
        return "failed"
    if job.get("queued"):
        return "store_busy"      # another idea's worker holds the lock; this one starts after it
    return "running"


def response(cfg, job: dict[str, Any], items: list[dict[str, Any]] | None = None, *,
             extra: dict[str, Any] | None = None) -> dict[str, Any]:
    a = job.get("approval")
    spent = spent_usd(cfg, job) if a else None
    job = newest_view(cfg, job)
    items = pending(cfg, job, spent) if items is None else items
    status = derive_status(cfg, job, items)
    res = job.get("result") or {}
    rem = remaining_usd(cfg, job, spent=spent) if a else None
    totals = idea_totals(cfg, job["idea"], job) if res else None
    failure = job.get("failure") or {}
    out: dict[str, Any] = {
        "command": "quickstart", "format": FORMAT, "status": status, "exit_code": STATUS_EXIT[status],
        "idea": job["idea"], "idea_key": job["idea_key"], "lang": job.get("lang"), "state": job.get("state"),
        "phase": (job.get("progress") or {}).get("phase"), "progress": job.get("progress"),
        "pending": items, "idea_en": job.get("idea_en"), "idea_en_source": job.get("idea_en_source"),
        "approved_usd": a["usd"] if a else None, "spent_usd": None if not a else round(a["usd"] - (rem or 0), 6),
        "remaining_usd": rem, "next_command": None, "poll_command": None, "retry_after": failure.get("retry_after"),
        "relay_every_s": RELAY_EVERY_S, "run_id": res.get("run_id"), "page": res.get("page"),
        "page_uri": Path(res["page"]).resolve().as_uri() if res.get("page") else None,
        "page_opened": bool(res.get("page_opened")), "deck_id": res.get("deck_id"), "cost_usd": res.get("cost_usd"),
        "total_cost_usd": (totals or {}).get("cost_usd"), "total_seconds": (totals or {}).get("seconds"),
        "version": res.get("version"), "change_zh": res.get("change_zh"), "change_en": res.get("change_en"),
        "idea_en_changes": job.get("idea_en_changes") or [],
        "timing": {s["id"]: s.get("seconds") for s in job.get("steps") or [] if s.get("seconds") is not None},
        "summary": res.get("summary"), "top": res.get("top") or [],
        "next_steps": [n for n in res.get("next_steps") or []      # the fill is its own question (fill_descriptions)
                       if not (job.get("fill_offer") and str(n.get("command") or "").startswith(
                           "jevscreen crawl-descriptions"))],
        "gaps": res.get("gaps") or [], "fetch": res.get("fetch"),
        "translation": res.get("translation"),
        "translation_pending": int((res.get("translation") or {}).get("pending") or 0),
        "error": failure.get("error") if status not in ("running",) else None, "notes": job.get("notes") or [],
    }
    if status in ("needs_human", "needs_agent"):
        out["intro_zh"], out["intro_en"] = intro_text(job, items, "zh"), intro_text(job, items, "en")
        out["before_you_start_zh"], out["before_you_start_en"] = STRINGS["zh"]["before"], STRINGS["en"]["before"]
        out["next_command"] = front_command(job)
    if job.get("state") == "running":
        # the worker runs (free downloads, the fill ...) even while the human or the agent still has to answer
        out["poll_command"] = status_command(job)
    if any(i.get("id") == "fill_descriptions" for i in items):
        out["next_command"] = front_command(job)       # plus the item's rerun_with / decline_with
    if status in ("failed", "store_busy", "ai_unavailable"):
        out["next_command"] = front_command(job, *(("--retry",) if failure.get("kind") in ("network", "fd")
                                                   and job.get("state") == "failed" else ()))
    if status == "store_busy" and job.get("queued"):
        out["poll_command"] = status_command(job)
    if status == "budget_exhausted":
        out["next_command"] = front_command(job)       # plus the top-up item's rerun_with after a yes
    for lang in ("zh", "en"):
        out[f"text_{lang}"] = human_text(job, status, items, lang, rem, totals)
    if job.get("idea_en_problems") and not job.get("idea_en"):
        out["idea_en_problems"] = job["idea_en_problems"]
        last = job.get("idea_en_last_refused") or {}
        out["refused_idea_en"] = last.get("text")
        out["suggested_idea_en"] = last.get("suggested")
        out["rerun_command"] = front_command(job, idea_en=last.get("suggested") or "<sentence>")
    out.update(extra or {})
    return out


def intro_text(job: dict[str, Any], items: list[dict[str, Any]], lang: str) -> str:
    """What happens next (only before the first result) and what the human still has to do, counted from the
    pending items: decisions (ask_human) and the key (human_action)."""
    T = STRINGS[lang]
    d = sum(1 for i in items if i.get("ask_human"))
    k = sum(1 for i in items if i.get("human_action"))
    if lang == "zh":
        n = {1: "一", 2: "两", 3: "三", 4: "四"}.get(d, str(d))
        need = T["need_dk"].format(n=n) if d and k else T["need_d"].format(n=n) if d else T["need_k"] if k else ""
    else:
        kw = {"N": {1: "One", 2: "Two", 3: "Three", 4: "Four"}.get(d, str(d)), "s": "" if d == 1 else "s",
              "are": "is" if d == 1 else "are"}
        need = T["need_dk"].format(**kw) if d and k else T["need_d"].format(**kw) if d else T["need_k"] if k else ""
    first = T["intro"] if not job.get("result") else ""
    return (first + (" " if lang == "en" and first and need else "") + need).strip()


def _duration_text(secs: float | None, lang: str) -> str:
    if secs is None:
        return "?"
    secs = int(round(secs))
    if lang == "zh":
        return f"{secs // 60} 分 {secs % 60} 秒" if secs >= 60 else f"{secs} 秒"
    return f"{secs // 60} min {secs % 60} s" if secs >= 60 else f"{secs} s"


def top_evidence(T: dict[str, str], r: dict[str, Any]) -> str:
    """The evidence words of one chat line, the same claim the page makes: the user's call, a gap (none of the
    text the AI read has the idea's words), or the evidence kind; then 'your call' / 'borderline' marks."""
    kind = r.get("evidence_kind") or "profile"
    if r.get("verdict_from_user"):
        ev = T["ev_user"]
    elif r.get("excerpt_mentions_idea") is False:
        ev = T.get(f"ev_gap_{kind}", T["ev_gap_annual_report"])
    else:
        ev = T.get(f"ev_{kind}", kind)
    if r.get("user") and not r.get("verdict_from_user"):
        ev += T["mark_user"]
    if r.get("edge"):
        ev += T["mark_edge"]
    return ev


def human_text(job: dict[str, Any], status: str, items: list[dict[str, Any]], lang: str,
               rem: float | None, totals: dict[str, Any] | None = None) -> str:
    T = STRINGS[lang]
    res = job.get("result") or {}
    failure = job.get("failure") or {}
    fixes = [T["idea_en_fix"].format(old=c["old"], new=c["new"], words=", ".join(c.get("words") or []) or "?")
             for c in job.get("idea_en_changes") or [] if c.get("kind") == "false_positive_fix"]
    if status == "declined":
        return T["declined"]
    if status in ("done", "partial", "budget_exhausted") or (status == "running" and res.get("run_id")):
        s = res.get("summary") or {}
        c = float((totals or {}).get("cost_usd") if (totals or {}).get("cost_usd") is not None
                  else (res.get("cost_usd") or 0.0))
        where = T["opened"] if res.get("page_opened") else T["open_file"].format(
            uri=Path(res["page"]).resolve().as_uri() if res.get("page") else "?")
        secs = (totals or {}).get("seconds") if (totals or {}).get("seconds") else res.get("seconds")
        t = _duration_text(secs, lang)
        lines = []
        if status == "running":
            lines.append(_progress_text(job, lang) or "")
        if res.get("reused"):
            lines.append(T["reused"])
        if status == "budget_exhausted":
            lines.append(T["budget_exhausted"].format(n=s.get("listed", 0), x=_usd(job.get("topup_usd") or 0.25)))
        lines.append(T["done"].format(n=s.get("listed", 0), a=s.get("annual_report", 0), p=s.get("profile_only", 0),
                                      c=_usd(c), y=_cny_words(c), t=t, where=where))
        if (res.get("version") or 1) > 1 and res.get(f"change_{lang}"):
            lines.append(T["done_version"].format(v=res["version"], change=res[f"change_{lang}"]))
        lines += fixes
        fill = job.get("fill") or {}
        if fill.get("finished_at") and fill.get(f"message_{lang}"):
            lines.append(fill[f"message_{lang}"])
        elif fill.get("finished_at") and fill.get("answer") == "yes" and not fill.get("added"):
            lines.append(T["fill_none"])
        for it in items:
            if it.get("id") == "fill_descriptions":
                lines.append(it[f"question_{lang}"])
        from .page import STRINGS as PAGE_STRINGS
        for r in res.get("top") or []:
            # an agent-translated name keeps the original beside it; a translated line is marked, as on the page
            ticker = (f"{r['name_en']}{'，' if lang == 'zh' else ', '}{r['ticker']}"
                      if r.get("name_translated") and r.get("name_en") else r["ticker"])
            one = (r.get("one_line") or "-") + (PAGE_STRINGS[lang]["text_ai"] if r.get("one_line_translated") else "")
            lines.append(T["top_line"].format(rank=r["rank"], name=r["name"], ticker=ticker,
                                              country=(lang == "zh" and r.get("country_zh")) or r.get("country")
                                              or "?", one=one,
                                              verdict=r[f"verdict_words_{lang}"], evidence=top_evidence(T, r)))
        steps = [n for n in res.get("next_steps") or []
                 if not (job.get("fill_offer") and str(n.get("command") or "").startswith("jevscreen crawl-descriptions"))]
        for i, ns in enumerate(steps, 1):
            lines.append(f"{'下一步' if lang == 'zh' else 'Next'} {i}{'：' if lang == 'zh' else ': '}{ns[f'text_{lang}']}"
                         + (f"  ({ns['command']})" if ns.get("command") else ""))
        return "\n".join(lines)
    if status in ("needs_human", "needs_agent"):
        lines = [intro_text(job, items, lang)]
        n = 0
        for it in items:
            if it.get("ask_agent"):
                continue
            n += 1
            lines.append(f"{n}. " + (it.get(f"question_{lang}") or it.get(f"text_{lang}") or "")
                         + ((" " if lang == "en" else "") + it[f"note_{lang}"] if it.get(f"note_{lang}") else ""))
        lines += fixes
        if job.get("state") == "running" and not job.get("result"):
            lines.append(T["bg_started"])
        return "\n".join(x for x in lines if x)
    if status == "running":
        return _progress_text(job, lang) or T["running"].format(phase=1, what=T["p_check"])
    kind = failure.get("kind")
    if job.get("state") == "interrupted":
        return T["interrupted"]
    if kind == "blocked" and failure.get("source") == "fd":
        from .sources import financedatabase_local as fd
        return T["fd_blocked"].format(retry_after=failure.get("retry_after") or "24 h", **_fd_urls(lang))
    if kind == "blocked":
        return T["tv_blocked"].format(retry_after=failure.get("retry_after") or "24 h")
    if job.get("queued") and status == "store_busy" and fill_waiting(job):
        o = (job.get("fill") or {}).get("offer") or job.get("fill_offer") or {}
        return T["fill_queued"].format(m=o.get("minutes", "?"))
    if job.get("queued") and status == "store_busy":
        return T["queued"]
    if kind == "busy":
        return T["store_busy"]
    if kind == "ai_unavailable":
        pr = _provider(failure.get("provider"))
        key = ("no_credit" if failure.get("http_status") == 402 else
               "key_rejected_vercel" if pr.name == "vercel" and failure.get("http_status") == 403 else "key_rejected")
        text = T[key].format(label=provider_label(pr, lang), credits_url=pr.credits_url)
        return zh_tidy(text) if lang == "zh" else text
    if kind == "fd":
        return T["fd_failed"].format(**_fd_urls(lang))
    if kind == "network":
        return T["net_down"].format(t=failure.get("retry_after") or "15 min")
    if kind == "python":
        return T["python_old"]
    reason = failure.get("reason")
    if reason and f"fail_{reason}" in T:
        return T[f"fail_{reason}"]
    return T["failed"].format(error=failure.get("error") or "?")


def _fd_urls(lang: str) -> dict[str, str]:
    """The manual download URLs, the one most likely to open first (jsDelivr in mainland China, GitHub elsewhere)."""
    from .sources import financedatabase_local as fd
    gh, cdn = fd.FD_MIRRORS[0], fd.FD_MIRRORS[-1]
    return {"url": cdn, "url2": gh} if lang == "zh" else {"url": gh, "url2": cdn}


# ------------------------------------------------------------------------------------------------ front

def _stable_holds(cfg, job: dict[str, Any], run_id: str | None) -> bool:
    from . import page
    return page.stable_holds(cfg, job["idea"], run_id)


def _heartbeat_age(job: dict[str, Any]) -> float | None:
    hb = parse_iso(((job.get("worker") or {}).get("heartbeat_at")))
    return None if hb is None else (now_utc() - hb).total_seconds()


def _free_work_left(job: dict[str, Any]) -> bool:
    return any(not step_done(job, s) for s in FREE_STEPS)


def _backoff_active(job: dict[str, Any]) -> bool:
    f = job.get("failure") or {}
    ra = parse_iso(f.get("retry_after"))
    return job.get("state") == "failed" and f.get("kind") in ("blocked", "network", "fd") and ra is not None \
        and ra > now_utc()


def reusable(cfg, job: dict[str, Any]) -> bool:
    res = job.get("result") or {}
    sv = _sieve(cfg, job["idea"])
    return job.get("state") == "done" and bool(res.get("run_id")) and res.get("status") in ("ok", "partial") \
        and res.get("sieve_version") == (sv or {}).get("version") \
        and res.get("idea_en") == job.get("idea_en") and res.get("min_mcap_usd") == job.get("min_mcap_usd") \
        and res.get("countries") == job.get("countries")


def front(cfg, idea: str, *, idea_en: str | None = None, approve_budget: float | None = None,
          min_mcap: float | None = None, countries: list[str] | None = None, lang: str = "auto",
          fd_file: str | None = None, no_open: bool = False, retry: bool = False, new_run: bool = False,
          fill_descriptions: str | None = None,
          spawn: Callable[[Any, str], Any] | None = None) -> dict[str, Any]:
    """The front command (fast, no network). Returns the quickstart JSON (with exit_code)."""
    from . import guard
    idea = (idea or "").strip()
    if not idea:
        raise ValueError("the idea must not be empty")
    key = idea_key(idea)
    flags: dict[str, Any] = {k: v for k, v in (("idea_en", idea_en), ("approve_budget", approve_budget),
                                               ("min_mcap_usd", min_mcap), ("countries", countries),
                                               ("fd_file", fd_file), ("no_open", no_open or None),
                                               ("retry", retry or None), ("new_run", new_run or None),
                                               ("fill_descriptions", fill_descriptions),
                                               ("lang", lang if lang in ("zh", "en") else None)) if v is not None}
    try:
        lock = guard.budget_lock(cfg, LOCK)
        lock.__enter__()
    except guard.Busy:
        return _front_busy(cfg, idea, key, flags, lang=lang, min_mcap=min_mcap, countries=countries)
    try:
        job = load_job(cfg, key) or new_job(idea, lang=lang if lang in ("zh", "en") else detect_lang(idea),
                                           min_mcap=min_mcap if min_mcap is not None else DEFAULT_MIN_MCAP,
                                           countries=countries)
        merged = {**take_inbox(cfg, key), **flags}
        resolve_idea_en(cfg, job)
        problems = apply_flags(cfg, job, merged)
        resolve_idea_en(cfg, job)
        sieve_drift(cfg, job, merged)
        if job.get("state") == "running":
            age = _heartbeat_age(job)
            if age is None or age > HEARTBEAT_STALE_S:
                job["state"] = "interrupted"
                job["notes"] = (job.get("notes") or []) + [f"worker stopped without finishing ({iso(now_utc())})"]
        c = consent_state(cfg)
        if c["state"] == "no":
            job["state"] = "declined"
            save_job(cfg, job)
            return _with_refusal(response(cfg, job, []), job, problems)
        if job.get("state") == "declined":
            job["state"] = "new"
        # a 402 is retried when the agent reruns the front ("tell me 'done'"); 401/403 only with a new key
        canary = job.get("canary") or {}
        if canary.get("status") == 402 or (canary.get("status") in (401, 403)
                                           and canary.get("fingerprint") != key_state(cfg)["fingerprint"]):
            job["canary"] = None
            reset_steps(job, "ai_check")
            if (job.get("failure") or {}).get("kind") == "ai_unavailable":
                job.update(failure=None, state="new")
        items = pending(cfg, job)
        fill = job.get("fill") or {}
        if job.get("state") == "done" and fill.get("answer") == "yes" and not fill.get("finished_at") \
                and consent_current(c) and not job.get("reprice"):
            # the yes to the optional profile fill: the worker fills and re-ranks in the background; the current
            # result stays in the job (and on the page) meanwhile
            job["state"] = "running"
            t = iso(now_utc())
            job["worker"] = {"pid": None, "started_at": t, "heartbeat_at": t}
            job["progress"] = {**(job.get("progress") or {}), "heartbeat_at": t}
            save_job(cfg, job)
            lock.__exit__(None, None, None)
            lock = None
            (spawn or spawn_worker)(cfg, key)
            return _with_refusal(response(cfg, job), job, problems)
        if job.get("state") == "done" and (reusable(cfg, job) or exhausted(job)):
            save_job(cfg, job)           # the result as it is ($0); an exhausted one carries the top-up question
            return _with_refusal(response(cfg, job), job, problems)
        if job.get("state") == "done":        # sieve / scope changed: screen again (the Jev cache makes it cheap)
            reset_steps(job, "estimate", "screen", "fetch", "finish")
            job.update(state="new", result=None)
            items = pending(cfg, job)
        consent_ok = consent_current(c)
        worker_alive = job.get("state") == "running"
        can_work = consent_ok and not _backoff_active(job) and (
            _free_work_left(job) or not items)
        if can_work and not worker_alive and job.get("state") != "done":
            if job.get("state") == "failed":
                job["failure"] = None
            job["state"] = "running"
            t = iso(now_utc())
            job["worker"] = {"pid": None, "started_at": t, "heartbeat_at": t}
            job["progress"] = {**(job.get("progress") or {}), "heartbeat_at": t}
            save_job(cfg, job)
            lock.__exit__(None, None, None)
            lock = None
            (spawn or spawn_worker)(cfg, key)
        else:
            save_job(cfg, job)
    finally:
        if lock is not None:
            lock.__exit__(None, None, None)
    return _with_refusal(response(cfg, job), job, problems)


def _with_refusal(out: dict[str, Any], job: dict[str, Any], problems: list[str]) -> dict[str, Any]:
    """Every answer of the front carries this call's --idea-en refusal (also a reused or finished result)."""
    if problems:
        _refusal_fields(out, job, problems)
    return out


def _refusal_fields(out: dict[str, Any], job: dict[str, Any], problems: list[str]) -> None:
    """This call's --idea-en was refused: why, a rewrite without the names (or null) and the full command to run."""
    last = job.get("idea_en_last_refused") or {}
    out["idea_en_problems"] = problems
    out["refused_idea_en"] = last.get("text")
    out["suggested_idea_en"] = last.get("suggested")
    out["rerun_command"] = front_command(job, idea_en=last.get("suggested") or "<sentence>")


def _front_busy(cfg, idea: str, key: str, flags: dict[str, Any], *, lang: str, min_mcap: float | None,
                countries: list[str] | None) -> dict[str, Any]:
    """The front when a worker holds the quickstart lock: the flags go to this idea's inbox (the next lock holder
    merges them), a first front of an idea also writes its job file (state 'new', so --status finds it). When
    this idea's own worker runs the answer is 'running'; when another idea's worker holds the lock and nothing
    is left to ask, it is 'store_busy' (queued): --status or a rerun starts it once the lock is free."""
    job = load_job(cfg, key)
    if job is None:
        job = new_job(idea, lang=lang if lang in ("zh", "en") else detect_lang(idea),
                      min_mcap=min_mcap if min_mcap is not None else DEFAULT_MIN_MCAP, countries=countries)
        save_job(cfg, job)       # nobody else writes this idea's file: the lock holder works on another idea
    if flags:
        write_inbox(cfg, key, flags)
    view = json.loads(json.dumps(job))
    resolve_idea_en(cfg, view)
    inbox = read_inbox(cfg, key)
    problems = apply_flags(cfg, view, inbox)
    resolve_idea_en(cfg, view)
    sieve_drift(cfg, view, inbox)
    if view.get("state") != "running":
        view["queued"] = True      # a fill yes on a finished job too: --status starts it once the lock is free
    out = response(cfg, view)
    if problems:
        _refusal_fields(out, view, problems)
    return out


def spawn_worker(cfg, key: str) -> Any:
    """Start the worker detached (new session / process group), output to <home>/quickstart/<key>.log."""
    env = dict(os.environ)
    env["JEVSCREEN_HOME"] = str(cfg.home)
    src = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    log = log_path(cfg, key)
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log, "ab")
    kw: dict[str, Any] = {"stdout": fh, "stderr": fh, "stdin": subprocess.DEVNULL, "env": env, "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0x8) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
    else:
        kw["start_new_session"] = True
    try:
        return subprocess.Popen([sys.executable, "-m", "jevscreen.quickstart", "--worker", key], **kw)
    finally:
        fh.close()


def _resumable(cfg, job: dict[str, Any]) -> bool:
    """A job no worker runs that waits on nothing any more (the key was set after the worker stopped at it, flags
    were left in the inbox after the worker's last merge, a queued idea): the front's own bookkeeping restarts it."""
    if job.get("state") == "done":
        # a yes to the profile fill left in the inbox while another worker held the lock (or recorded but not
        # started): the front merges it and starts the fill; nothing else restarts a finished job here
        fd = read_inbox(cfg, job["idea_key"]).get("fill_descriptions")
        return (fd == "yes" and bool(job.get("fill_offer")) and not (job.get("fill") or {}).get("answer")) \
            or (fill_waiting(job) and consent_current(consent_state(cfg)))
    if job.get("state") not in ("new", "waiting"):
        return False
    if read_inbox(cfg, job["idea_key"]):
        return True
    c = consent_state(cfg)
    if not consent_current(c):
        return False
    return not pending(cfg, job)


def _view(cfg, key: str, spawn: Callable[[Any, str], Any] | None) -> dict[str, Any] | None:
    job = load_job(cfg, key)
    if job is None:
        return None
    if job.get("state") == "running" and (_heartbeat_age(job) or 0) > HEARTBEAT_STALE_S:
        job = {**job, "state": "interrupted"}
    if _resumable(cfg, job):
        return front(cfg, job["idea"], spawn=spawn)
    return response(cfg, job)


def status(cfg, key: str | None = None, *, idea: str | None = None, wait_s: float = 0.0,
           sleep: Callable[[float], None] = time.sleep,
           spawn: Callable[[Any, str], Any] | None = None) -> dict[str, Any] | None:
    """The job's JSON; with wait_s (<= WAIT_MAX_S) blocks until the state or the pending ids change. A job that
    waits on nothing any more (see _resumable) is started again here, exactly as the front would (no network in
    this process); status never starts anything that needs an answer."""
    wait_s = max(0.0, min(float(wait_s or 0.0), WAIT_MAX_S))
    deadline = time.monotonic() + wait_s
    if key is None and idea:
        key = idea_key(idea.strip())
    if key is None:
        files = sorted(job_dir(cfg).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) \
            if job_dir(cfg).is_dir() else []
        files = [p for p in files if not p.name.endswith(".flags.json") and p.name != names_cache_path(cfg).name
                 and load_job(cfg, p.stem) is not None]       # the names cache is not a job
        if not files:
            return None
        key = files[0].stem
    first = _view(cfg, key, spawn)
    if first is None:
        return None

    def waitable(o: dict[str, Any]) -> bool:
        return o["status"] == "running" or (o["status"] == "store_busy" and o.get("state") in ("new", "waiting",
                                                                                                "done"))
    if wait_s <= 0 or not waitable(first):
        return first
    sig = (first["status"], first["state"], tuple(i["id"] for i in first["pending"]))
    out = first
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        sleep(min(1.0, left))
        if time.monotonic() >= deadline:
            break
        out = _view(cfg, key, spawn) or out
        if (out["status"], out["state"], tuple(i["id"] for i in out["pending"])) != sig or not waitable(out):
            break
    return out


# ------------------------------------------------------------------------------------------------ worker

@dataclasses.dataclass
class Deps:
    """Everything the worker calls that touches the network or Jev; tests replace these with fakes."""
    jev_factory: Callable | None = None                    # screen(jev_factory=...); None = JevClient
    keywords_fn: Callable | None = None
    scanner_client: Callable[[Any], Any] | None = None     # cfg -> polite http.Client (1 req/s)
    refresh_universe: Callable | None = None               # (cfg, con, client) -> summary
    fd_client: Callable[[Any], Any] | None = None
    download_equities: Callable | None = None              # (cfg, client) -> info
    import_descriptions: Callable | None = None            # (cfg, con, fd_csv) -> summary
    check_jev: Callable[[Any], dict] | None = None         # cfg -> doctor check dict (free)
    canary: Callable[[Any], dict] | None = None            # cfg -> {'status': 'ok'|402|..., 'cost_usd'}
    fetch_docs: Callable | None = None     # (cfg, result, *, time_s, update_budget, on_progress, out, jev_factory)
                                           # -> ondemand_cli.run_fetch's dict; None = the real on-demand fetch
    open_page: Callable[[Path], bool] | None = None
    pack_pull: Callable | None = None                      # (cfg, repo) -> summary; None = jevscreen.pack
    crawl_client: Callable[[Any], Any] | None = None       # cfg -> the polite crawl client (cli.make_crawl_client)
    crawl_descriptions: Callable | None = None             # (cfg, client, *, countries, min_mcap_usd) -> summary


DEPS = Deps()


def _default_scanner_client(cfg):
    from . import cli
    return cli.make_client(cfg)


def _default_fd_client(cfg):
    from .http import Client
    from .sources import financedatabase_local as fd
    return Client(user_agent=cfg.user_agent, min_interval_s=1.0, timeout_s=fd.MIRROR_DEADLINE_S)


def _default_refresh(cfg, con, client):
    from .sources import tradingview_scanner
    return tradingview_scanner.refresh_universe(cfg, con, client, with_history=False)


def _default_download(cfg, client):
    from .sources import financedatabase_local as fd
    return fd.download_equities(cfg, client)


def _default_import(cfg, con, fd_csv):
    from .sources import financedatabase_local as fd
    return fd.import_descriptions(cfg, con, fd_csv=fd_csv)


def _default_check_jev(cfg):
    from . import doctor
    return doctor.check_jev(cfg)


CANARY_TEXT = ("Northwind Paper Cups Ltd makes disposable paper cups and lids for coffee shops. Reference {nonce}.")


def _default_canary(cfg) -> dict[str, Any]:
    """One paid request (a fixed one-item question, budget CANARY_BUDGET_USD) that proves the key can pay. The item
    text carries a nonce so the reuse cache never answers it for free."""
    from . import jev
    nonce = hashlib.sha256(f"{time.time_ns()}-{os.getpid()}".encode()).hexdigest()[:10]
    client = jev.JevClient(cfg, run_id=f"canary-{now_utc().date().isoformat()}", layer="canary",
                           budget_usd=CANARY_BUDGET_USD, pack_size=1, workers=1)
    q = jev.Question(key="canary", instructions="Does state.text say that state.issuer makes paper cups?",
                     criteria={"yes": "The text says so.", "no": "The text does not say so."})
    item = jev.Item(item_id="canary/1", issuer="Northwind Paper Cups Ltd", text=CANARY_TEXT.format(nonce=nonce))
    try:
        res = client.classify([item], q)
    except jev.JevBusy:
        return {"status": "busy", "cost_usd": 0.0}
    except jev.JevUnavailable as e:
        m = re.search(r"HTTP (40[123])", str(e))
        return {"status": int(m.group(1)) if m else "unavailable", "cost_usd": round(client.spent_usd, 8),
                "error": str(e)[:200]}
    st = (res[0] or {}).get("status") if res else None
    return {"status": "ok" if st == "ok" else (st or "failed"), "cost_usd": round(client.spent_usd, 8)}


def _default_fetch_docs(cfg, result, **kw):
    """The one on-demand path (jevscreen.ondemand_cli.run_fetch, as `screen --fetch-docs auto`): annual reports of
    the run's profile-only layer-2 companies, then the update pass; SIGTERM stops it (interrupt_stops)."""
    from . import ondemand_cli
    return ondemand_cli.run_fetch(cfg, result, mode="auto", interrupt_stops=True, **kw)


def _default_open(path: Path) -> bool:
    from . import page
    return page.open_page(path)


def _pack_repo() -> str | None:
    try:
        from . import pack
    except Exception:  # noqa: BLE001 - the pack module is optional
        return None
    return os.environ.get("JEVSCREEN_PACK_REPO") or getattr(pack, "DEFAULT_PACK_REPO", "") or None


class Worker:
    """One worker run over a job (the caller holds the quickstart lock)."""

    def __init__(self, cfg, job: dict[str, Any], deps: Deps) -> None:
        self.cfg, self.job, self.d = cfg, job, deps
        self.mu = threading.RLock()
        self.last_progress = 0.0
        self.stop = threading.Event()
        self.t_start = time.monotonic()
        self.base_seconds = float(job.get("worker_seconds") or 0.0)

    def tick(self) -> None:
        """job['worker_seconds']: the background work's own clock over every worker run of this idea (the time
        waiting for the human between runs is not in it)."""
        with self.mu:
            self.job["worker_seconds"] = round(self.base_seconds + time.monotonic() - self.t_start, 1)

    def add_run(self, run_id: str | None) -> None:
        if run_id:
            with self.mu:
                ids = self.job.setdefault("run_ids", [])
                if run_id not in ids:
                    ids.append(run_id)

    # -- job bookkeeping
    def save(self) -> None:
        with self.mu:
            save_job(self.cfg, self.job)

    def beat(self) -> None:
        with self.mu:
            self.tick()
            t = iso(now_utc())
            self.job["worker"]["heartbeat_at"] = t
            self.job.setdefault("progress", {})["heartbeat_at"] = t
            self.save()

    def heartbeat_loop(self) -> None:
        while not self.stop.wait(HEARTBEAT_EVERY_S):
            with contextlib.suppress(Exception):
                self.beat()

    def progress(self, phase: int, text_zh: str, text_en: str, done: int | None = None, total: int | None = None,
                 force: bool = False) -> None:
        with self.mu:
            p = self.job.get("progress") or {}
            changed = p.get("phase") != phase
            self.job["progress"] = {"phase": phase, "done": done, "total": total, "text_zh": text_zh,
                                    "text_en": text_en, "heartbeat_at": iso(now_utc())}
            if force or changed or time.monotonic() - self.last_progress >= PROGRESS_EVERY_S:
                self.last_progress = time.monotonic()
                self.job["worker"]["heartbeat_at"] = iso(now_utc())
                self.save()

    def record(self, sid: str, status: str, t0: float, detail_zh: str = "", detail_en: str = "",
               **extra: Any) -> None:
        with self.mu:
            reset_steps(self.job, sid)
            self.job["steps"].append({"id": sid, "status": status, "started_at": extra.pop("started_at", None),
                                      "seconds": round(time.monotonic() - t0, 2), "detail_zh": detail_zh,
                                      "detail_en": detail_en, **extra})
            self.save()

    def fail(self, kind: str, error: str | None = None, **extra: Any) -> str:
        with self.mu:
            self.job["failure"] = {"kind": kind, "error": error, "at": iso(now_utc()), **extra}
            self.job["state"] = "failed"
            self.save()
        return "stop"

    def wait(self, item: str, kind: str | None = None) -> str:
        with self.mu:
            self.job["waiting_on"] = item
            self.job["waiting_kind"] = kind
            self.job["state"] = "waiting"
            self.save()
        return "stop"

    def net_failure(self, sid: str, error: str) -> None:
        with self.mu:
            ra = iso(now_utc() + dt.timedelta(seconds=RETRY_NET_S))
            self.job.setdefault("failures", {})[sid] = {"at": iso(now_utc()), "error": error[:300],
                                                        "retry_after": ra}
            self.save()

    def backoff(self, sid: str) -> bool:
        f = (self.job.get("failures") or {}).get(sid) or {}
        ra = parse_iso(f.get("retry_after"))
        return ra is not None and ra > now_utc()

    def merge_inbox(self) -> None:
        flags = take_inbox(self.cfg, self.job["idea_key"])
        if flags:
            with self.mu:
                resolve_idea_en(self.cfg, self.job)
                apply_flags(self.cfg, self.job, flags)
                resolve_idea_en(self.cfg, self.job)
                self.save()

    # -- store facts
    def universe_facts(self) -> dict[str, Any]:
        from . import store
        if not Path(self.cfg.db_path).exists():
            return {"companies": 0, "age_days": None, "at_floor": 0}
        with store.session(self.cfg, read_only=True, wait_s=60.0) as con:
            n, as_of = con.execute("SELECT count(*), max(market_as_of) FROM universe").fetchone()
            at_floor = con.execute("SELECT count(*) FROM universe WHERE market_cap_usd >= ?",
                                   [float(self.job["min_mcap_usd"])]).fetchone()[0]
        if isinstance(as_of, dt.datetime):
            as_of = as_of.date()
        age = (now_utc().date() - as_of).days if isinstance(as_of, dt.date) else None
        return {"companies": int(n or 0), "age_days": age, "at_floor": int(at_floor or 0)}

    def description_facts(self) -> dict[str, Any]:
        from . import store
        from .sources import financedatabase_local as fd
        with store.session(self.cfg, read_only=True, wait_s=60.0) as con:
            total, described = con.execute(
                """WITH u AS (SELECT security_id, company_key FROM universe WHERE market_cap_usd >= ?)
                   SELECT count(*), count(*) FILTER (WHERE EXISTS (
                       SELECT 1 FROM descriptions d WHERE (d.company_key = u.company_key OR d.security_id =
                       u.security_id) AND d.text IS NOT NULL AND trim(d.text) <> '')) FROM u""",
                [float(self.job["min_mcap_usd"])]).fetchone()
            recent = con.execute("SELECT max(fetched_at) FROM snapshots WHERE source_id = ? AND kind = ?",
                                 [fd.SOURCE_ID, fd.BZ2_KIND]).fetchone()[0]
        share = (described / total) if total else 0.0
        fresh = recent is not None and (now_utc() - recent) <= dt.timedelta(days=DESC_IMPORT_FRESH_DAYS)
        return {"companies": int(total or 0), "described": int(described or 0), "share": share,
                "import_fresh": bool(fresh)}

    # -- steps
    def step_check(self) -> str:
        from . import doctor, store
        t0 = time.monotonic()
        T = STRINGS
        self.progress(1, T["zh"]["running"].format(phase=1, what=T["zh"]["p_check"]),
                      T["en"]["running"].format(phase=1, what=T["en"]["p_check"]), force=True)
        if sys.version_info < (3, 10):
            return self.fail("python", "Python < 3.10")
        if not Path(self.cfg.db_path).exists():
            with store.session(self.cfg, wait_s=120.0):
                pass
        rep = doctor.run(self.cfg)
        checks = {c["id"]: c for c in rep["checks"]}
        if (checks.get("dep_duckdb") or {}).get("status") == "fail":
            return self.fail("failed", "duckdb is not installed", reason="duckdb")
        if (checks.get("dep_pymupdf") or {}).get("status") == "warn" and "no PDF library" in \
                (checks["dep_pymupdf"].get("detail") or ""):
            with self.mu:
                note = ("no PDF library: China / India / Taiwan annual reports are not fetched on demand "
                        "(python3 -m pip install pypdf)")
                if note not in self.job["notes"]:
                    self.job["notes"].append(note)
        self.record("check", "ok", t0, T["zh"]["p_check_ok"], T["en"]["p_check_ok"])
        self.progress(1, T["zh"]["p_check_ok"], T["en"]["p_check_ok"], force=True)
        return "ok"

    def step_universe(self) -> str:
        from . import ops, store
        t0 = time.monotonic()
        T = STRINGS
        facts = self.universe_facts()
        floor = f"${float(self.job['min_mcap_usd']) / 1e9:g}B"
        if facts["companies"] and facts["age_days"] is not None and facts["age_days"] <= UNIVERSE_FRESH_DAYS:
            zh = T["zh"]["p_universe_skip"].format(age=UNIVERSE_FRESH_DAYS)
            en = T["en"]["p_universe_skip"].format(age=UNIVERSE_FRESH_DAYS)
            self.record("universe", "skipped", t0, zh, en)
            self.progress(2, zh, en, force=True)
            return "ok"
        if self.backoff("universe"):
            if facts["companies"]:
                self.record("universe", "skipped", t0, "网络失败后 15 分钟内不重试，先用旧清单", "not retried within 15 min "
                            "of a network failure; using the old list")
                return "ok"
            f = self.job["failures"]["universe"]
            return self.fail("network", f.get("error"), retry_after=f.get("retry_after"))
        self.progress(2, T["zh"]["running"].format(phase=2, what=T["zh"]["p_universe"]),
                      T["en"]["running"].format(phase=2, what=T["en"]["p_universe"]), force=True)
        client = (self.d.scanner_client or _default_scanner_client)(self.cfg)
        refresh = self.d.refresh_universe or _default_refresh

        def work():
            with store.session(self.cfg, wait_s=600.0) as con:
                return refresh(self.cfg, con, client)
        code, summ = ops.run_networked("refresh-universe", self.cfg, work, client=client,
                                       consent_source="tradingview_scanner")
        if code == 0:
            facts = self.universe_facts()
            zh = T["zh"]["p_universe_ok"].format(n=f"{facts['at_floor']:,}", floor=floor,
                                                 s=int(time.monotonic() - t0))
            en = T["en"]["p_universe_ok"].format(n=f"{facts['at_floor']:,}", floor=floor,
                                                 s=int(time.monotonic() - t0))
            self.record("universe", "ok", t0, zh, en, companies=facts["at_floor"])
            self.progress(2, zh, en, force=True)
            return "ok"
        if code == ops.EXIT_CONSENT:
            return self.fail("failed", "consent for gray-private sources is missing", reason="consent")
        if code == 2:
            if facts["companies"]:
                self.note("TradingView refused the refresh (24 h cooldown); screening the older stock list")
                self.record("universe", "skipped", t0, "TradingView 暂时拒绝，先用旧清单", "TradingView refused; "
                            "using the older list", blocked=True)
                return "ok"
            return self.fail("blocked", summ.get("reason") or summ.get("status"),
                             retry_after=summ.get("retry_after"))
        if code in (3, 4):
            return self.fail("busy", summ.get("status"))
        self.net_failure("universe", str(summ.get("error") or summ.get("status")))
        if facts["companies"]:
            self.record("universe", "skipped", t0, "下载失败，先用旧清单", "download failed; using the older list")
            return "ok"
        f = self.job["failures"]["universe"]
        return self.fail("network", f.get("error"), retry_after=f.get("retry_after"))

    def note(self, text: str) -> None:
        with self.mu:
            if text not in self.job["notes"]:
                self.job["notes"].append(text)

    def step_descriptions(self) -> str:
        from . import ops, store
        from .sources import financedatabase_local as fd
        t0 = time.monotonic()
        T = STRINGS
        facts = self.description_facts()
        pct = round(100 * facts["share"])
        fd_file = self.job.get("fd_file")
        if not fd_file and (facts["import_fresh"] or facts["share"] >= DESC_OK_SHARE):
            zh, en = T["zh"]["p_desc_skip"].format(pct=pct), T["en"]["p_desc_skip"].format(pct=pct)
            self.record("descriptions", "skipped", t0, zh, en, share=round(facts["share"], 3))
            self.progress(2, zh, en, force=True)
            return "ok"

        def gave_up(error: str, blocked_until: str | None = None) -> str:
            self.net_failure("descriptions", error)
            if blocked_until:
                with self.mu:
                    self.job["failures"]["descriptions"]["retry_after"] = blocked_until
            if facts["share"] >= DESC_FAIL_SHARE:
                self.note("company profiles could not be downloaded; screening the profiles already stored")
                self.record("descriptions", "skipped", t0, "下载失败，先用已有简介", "download failed; using the "
                            "stored profiles", share=round(facts["share"], 3))
                return "ok"
            f = self.job["failures"]["descriptions"]
            if blocked_until:     # the real cooldown, not the 15-minute network retry (--retry cannot shorten it)
                return self.fail("blocked", error, retry_after=blocked_until, source="fd")
            return self.fail("fd", error, retry_after=f.get("retry_after"))

        if not fd_file:
            if self.backoff("descriptions"):
                return gave_up((self.job["failures"]["descriptions"] or {}).get("error") or "earlier failure")
            self.progress(2, T["zh"]["running"].format(phase=2, what=T["zh"]["p_desc"]),
                          T["en"]["running"].format(phase=2, what=T["en"]["p_desc"]), force=True)
            client = (self.d.fd_client or _default_fd_client)(self.cfg)
            download = self.d.download_equities or _default_download
            code, summ = ops.run_networked("fetch-fd", self.cfg, lambda: download(self.cfg, client), client=client,
                                           consent_source=fd.SOURCE_ID)
            info = summ.get("result") or {}
            if code == 2:        # every mirror refused, now or within the 24 h fetch-fd cooldown
                return gave_up(f"blocked: {summ.get('reason') or summ.get('status')}",
                               blocked_until=summ.get("retry_after") or iso(now_utc() + dt.timedelta(hours=24)))
            if code in (3, 4):
                return self.fail("busy", summ.get("status"))
            if code != 0 or info.get("status") not in ("ok", "not_modified"):
                return gave_up(str(summ.get("error") or info.get("status") or "download failed")
                               + (f" ({'; '.join(info.get('errors') or [])})" if info.get("errors") else ""))
            fd_file = info["path"]
        imp = self.d.import_descriptions or _default_import

        def work():
            with store.session(self.cfg, wait_s=600.0) as con:
                return imp(self.cfg, con, fd_file)
        code, summ = ops.run_networked("import-fd", self.cfg, work, consent_source=fd.SOURCE_ID)
        if code in (3, 4):
            return self.fail("busy", summ.get("status"))
        if code != 0:
            return gave_up(str(summ.get("error") or summ.get("status")))
        facts = self.description_facts()
        pct = round(100 * facts["share"])
        zh = T["zh"]["p_desc_ok"].format(n=f"{facts['described']:,}", pct=pct, s=int(time.monotonic() - t0))
        en = T["en"]["p_desc_ok"].format(n=f"{facts['described']:,}", pct=pct, s=int(time.monotonic() - t0))
        with self.mu:
            self.job["failures"].pop("descriptions", None)
        self.record("descriptions", "ok", t0, zh, en, share=round(facts["share"], 3))
        self.progress(2, zh, en, force=True)
        return "ok"

    def step_pack(self) -> str:
        t0 = time.monotonic()
        T = STRINGS
        repo = _pack_repo()
        if not repo:
            self.record("pack", "skipped", t0, T["zh"]["p_pack_skip"], T["en"]["p_pack_skip"])
            return "ok"
        try:
            if self.d.pack_pull is not None:
                summ = self.d.pack_pull(self.cfg, repo)
            else:
                from . import guard, pack
                with guard.budget_lock(self.cfg, "api.github.com", reentrant=True):
                    summ = pack.pull(self.cfg, repo=repo)
        except Exception as e:  # noqa: BLE001 - optional step
            summ = {"status": "error", "note": f"{type(e).__name__}"}
        st = (summ or {}).get("status")
        self.record("pack", "ok" if st in ("ok", "already_imported") else "skipped", t0,
                    f"开放数据包：{st}", f"open data pack: {st}")
        return "ok"

    def check_idea_en(self, names: tuple[list[str], list[str]] | None = None) -> list[str]:
        """The front checks --idea-en against the universe's names (the store or the names cache). Only on the very
        first run is there nothing to check against: then it is checked here, right after the first stock list is
        in (after_universe, while the free downloads go on). A refused sentence is unpinned and the agent is asked
        for a new one; an approval given for it is held (not dropped): a fix of only the flagged word keeps it
        (apply_flags), any other new sentence is the reprice question."""
        from . import calib, store
        en, src = self.job.get("idea_en"), self.job.get("idea_en_source")
        if not en or src == "verbatim" or not Path(self.cfg.db_path).exists():
            return []
        if names is None:
            with store.session(self.cfg, read_only=True, wait_s=WORKER_DB_WAIT_S) as con:
                names = calib.load_names(con)
        probs = calib.idea_en_problems(en, self.job["idea"], names[0], names[1])
        if probs:
            hits = calib.idea_en_name_hits(en, self.job["idea"], names[0], names[1])
            with self.mu:
                self.job["idea_en_refused"] = list(dict.fromkeys((self.job.get("idea_en_refused") or []) + [en]))
                rec = {"text": en, "hits": hits, "problems": probs, "suggested": calib.suggest_idea_en(en, hits),
                       "at": iso(now_utc()), "by": "worker"}
                self.job["idea_en_last_refused"] = rec
                if approval_valid(self.job) and not self.job["approval"].get("out_dirs"):
                    self.job["idea_en_held"] = rec          # the human approved this English: held for a fix
                elif self.job.get("approval") and not self.job["approval"].get("out_dirs"):
                    self.job.update(approval=None, approval_agent=False)
                self.job.update(idea_en=None, idea_en_source=None, idea_en_agent=False, idea_en_problems=probs,
                                estimate=None)
                reset_steps(self.job, "idea_en", "estimate", "screen", "fetch", "finish")
                self.save()
        return probs

    def after_universe(self) -> None:
        """Right after the stock list is in: the names cache for the front's --idea-en check, then the one check the
        front could not make on a first run (no names yet). Never stops the free downloads that follow."""
        from . import calib, store
        try:
            with store.session(self.cfg, read_only=True, wait_s=WORKER_DB_WAIT_S) as con:
                names = calib.load_names(con)
        except Exception:  # noqa: BLE001 - checked again at the idea_en step
            return
        with contextlib.suppress(OSError):
            write_names_cache(self.cfg, *names)
        with contextlib.suppress(Exception):
            self.check_idea_en(names)

    def step_idea_en(self) -> str:
        t0 = time.monotonic()
        resolve_idea_en(self.cfg, self.job)
        try:
            if self.check_idea_en():
                resolve_idea_en(self.cfg, self.job)
                if self.check_idea_en() or not self.job.get("idea_en"):
                    return self.wait("idea_en")
        except store_locked() as e:
            return self.fail("busy", f"the database is busy: {str(e)[:120]}")
        if not self.job.get("idea_en"):
            return self.wait("idea_en")
        with self.mu:
            self.job["idea_en_problems"] = None
        self.record("idea_en", "ok", t0, self.job["idea_en"], self.job["idea_en"],
                    source=self.job.get("idea_en_source"))
        return "ok"

    def step_ai_check(self) -> str:
        t0 = time.monotonic()
        T = STRINGS
        k = key_state(self.cfg)
        if not k["configured"]:
            return self.wait("key_jev")
        canary = self.job.get("canary") or {}
        if canary.get("status") == "ok" and canary.get("fingerprint") == k["fingerprint"]:
            self.record("ai_check", "skipped", t0, "key 已验证", "key already checked")
            return "ok"
        chk = (self.d.check_jev or _default_check_jev)(self.cfg)
        if chk.get("status") == "fail":
            hs = chk.get("http_status")
            with self.mu:
                self.job["canary"] = {"at": iso(now_utc()), "status": hs or "no_key", "cost_usd": 0.0,
                                      "fingerprint": k["fingerprint"]}
            if hs in (401, 403) or not hs:
                return self.fail("ai_unavailable", chk.get("detail"), http_status=hs or 401, provider=k["provider"])
            return self.fail("ai_unavailable", chk.get("detail"), http_status=hs, provider=k["provider"])
        if not approval_valid(self.job):
            return self.wait("approve_budget", "first")
        self.progress(3, T["zh"]["running"].format(phase=3, what=T["zh"]["p_ai"]),
                      T["en"]["running"].format(phase=3, what=T["en"]["p_ai"]), force=True)
        res = (self.d.canary or _default_canary)(self.cfg)
        with self.mu:
            self.job["canary"] = {"at": iso(now_utc()), "status": res.get("status"),
                                  "cost_usd": float(res.get("cost_usd") or 0.0), "fingerprint": k["fingerprint"]}
            a = self.job["approval"]
            a["canary_usd"] = round(float(a.get("canary_usd") or 0.0) + float(res.get("cost_usd") or 0.0), 8)
            self.save()
        st = res.get("status")
        if st == "ok":
            self.record("ai_check", "ok", t0, T["zh"]["p_ai_ok"], T["en"]["p_ai_ok"], cost_usd=res.get("cost_usd"))
            self.progress(3, T["zh"]["p_ai_ok"], T["en"]["p_ai_ok"], force=True)
            return "ok"
        if st in (401, 402, 403):
            return self.fail("ai_unavailable", res.get("error"), http_status=st, provider=k["provider"])
        if st == "busy":
            return self.fail("busy", "another process is using Jev")
        return self.fail("failed", f"the test request failed ({st})", reason="canary")

    def dry_run(self, *, min_mcap: float, countries: list[str] | None, budget: float) -> dict[str, Any]:
        from . import screen
        with tempfile.TemporaryDirectory(prefix="jevscreen-estimate-") as tmp:
            res = screen.screen(self.cfg, self.job["idea"], idea_en=self.job["idea_en"], min_mcap_usd=min_mcap,
                                countries=countries, budget_usd=max(budget, 0.0001), reads=READS, dry_run=True,
                                out_dir=Path(tmp) / "estimate", sieve="auto", jev_factory=self.d.jev_factory,
                                keywords_fn=self.d.keywords_fn)
        b = res.get("dry_run_budget") or {}
        l1 = ((res.get("layers") or {}).get("l1") or {}).get("estimate") or {}
        return {"est_cost_usd": b.get("est_cost_usd"), "est_reserved_usd": b.get("est_reserved_usd"),
                "est_seconds": b.get("est_seconds"), "companies": (res.get("funnel") or {}).get("described"),
                "l1_usd": l1.get("est_cost_usd")}

    def alternatives(self, remaining: float) -> list[dict[str, Any]]:
        """Up to 3 narrower scopes, most relevant first: countries named in the idea; the idea's language market
        (zh: CN + HK, ja: JP, ko: KR); a higher floor ($5B)."""
        from . import screen
        idea = self.job["idea"]
        cands: list[tuple[dict[str, Any], str, str]] = []
        named = [iso2 for iso2, words in COUNTRY_WORDS.items() if any(w in idea for w in words)]
        if named:
            cands.append(({"countries": named}, "只看 " + "、".join(named), "only " + ", ".join(named)))
        c = screen.script_counts(idea)
        lang_c = ["CN", "HK"] if c.get("han", 0) and not c.get("kana") else ["JP"] if c.get("kana") else \
            ["KR"] if c.get("hangul") else None
        if lang_c and lang_c != named:
            cands.append(({"countries": lang_c}, "只看 " + "、".join(lang_c), "only " + ", ".join(lang_c)))
        cands.append(({"min_mcap_usd": 5e9}, "只看市值 ≥ $5B", "market cap ≥ $5B only"))
        out = []
        for args, zh, en in cands[:3]:
            try:
                e = self.dry_run(min_mcap=args.get("min_mcap_usd", float(self.job["min_mcap_usd"])),
                                 countries=args.get("countries", self.job.get("countries")), budget=remaining)
            except (ValueError, Exception):  # noqa: BLE001 - an unknown token etc.: not offered
                continue
            flag = (["--countries", ",".join(args["countries"])] if "countries" in args
                    else ["--min-mcap", _num(args["min_mcap_usd"])])
            if e.get("est_reserved_usd") is not None and float(e["est_reserved_usd"]) > remaining + 1e-9:
                continue            # still over the approval: picking it would only ask again
            out.append({"args": args, "flags": _command(*flag), "label_zh": zh, "label_en": en,
                        "estimate_usd": e["est_cost_usd"], "reserved_usd": e["est_reserved_usd"],
                        "companies": e["companies"]})
        return out

    def step_estimate(self) -> str:
        t0 = time.monotonic()
        T = STRINGS
        if not approval_valid(self.job):
            return self.wait("approve_budget", "first")
        try:
            rem = self.remaining()
        except store_locked() as e:
            return self.fail("busy", f"the database is busy: {str(e)[:120]}")
        self.progress(3, T["zh"]["running"].format(phase=3, what=T["zh"]["p_estimate"]),
                      T["en"]["running"].format(phase=3, what=T["en"]["p_estimate"]), force=True)
        try:
            est = self.dry_run(min_mcap=float(self.job["min_mcap_usd"]), countries=self.job.get("countries"),
                               budget=rem)
        except ValueError as e:
            return self.fail("failed", str(e)[:300], reason="screen_args")
        first = not (self.job["approval"].get("out_dirs") or [])
        with self.mu:
            self.job["estimate"] = {**est, "for": [self.job["idea_en"], self.job["min_mcap_usd"],
                                                   self.job.get("countries")], "alternatives": []}
        if first and float(est.get("est_reserved_usd") or 0.0) > rem + 1e-9:
            alts = self.alternatives(rem)
            with self.mu:
                self.job["estimate"]["alternatives"] = alts
            return self.wait("approve_budget", "over")
        zh = T["zh"]["p_estimate_ok"].format(est=_usd(est.get("est_cost_usd")), r=_usd(est.get("est_reserved_usd")))
        en = T["en"]["p_estimate_ok"].format(est=_usd(est.get("est_cost_usd")), r=_usd(est.get("est_reserved_usd")))
        self.record("estimate", "ok", t0, zh, en, est_cost_usd=est.get("est_cost_usd"),
                    est_reserved_usd=est.get("est_reserved_usd"))
        self.progress(3, zh, en, force=True)
        return "ok"

    def remaining(self) -> float:
        """The approval's remainder from the ledger (store.StoreLocked when it cannot be read: never a guess)."""
        with self.mu:
            rem = remaining_usd(self.cfg, self.job, strict=True) or 0.0
            self.save()                     # keeps approval.ledger_usd for the views
        return rem

    def uncertain(self) -> tuple[int, float]:
        """Items of this approval's runs whose send had an unknown outcome (a kill mid-screen). Raises
        store.StoreLocked when the store cannot be read (an unknown count is never taken as zero)."""
        from . import store
        dirs = (self.job.get("approval") or {}).get("out_dirs") or []
        if not dirs or not Path(self.cfg.db_path).exists():
            return 0, 0.0
        with store.session(self.cfg, read_only=True, wait_s=WORKER_DB_WAIT_S) as con:
            n = con.execute(
                "SELECT count(*) FROM jev_items i WHERE i.status IN ('uncertain', 'sent') AND i.run_id IN "
                "(SELECT run_id FROM screen_runs WHERE list_contains(?::VARCHAR[], output_dir))",
                [dirs]).fetchone()[0]
        return int(n or 0), round(int(n or 0) * UNCERTAIN_ITEM_USD, 6)

    def step_screen(self) -> str:
        from . import screen
        t0 = time.monotonic()
        T = STRINGS
        if not approval_valid(self.job):
            return self.wait("approve_budget", "first")
        try:
            rem = self.remaining()
            n_unc, c_unc = self.uncertain()
        except store_locked() as e:
            return self.fail("busy", f"the database is busy: {str(e)[:120]}")
        if rem < 0.01:
            with self.mu:
                self.job["topup_usd"] = 0.25
            return self.wait("approve_budget", "topup")
        retry_unc = False
        if n_unc:
            if c_unc <= UNCERTAIN_AUTO_USD and c_unc <= rem:
                retry_unc = True
                self.note(f"{n_unc} items with an unknown outcome resent (about ${c_unc:.4f})")
            elif not self.job.get("uncertain_ok"):
                with self.mu:
                    self.job.update(uncertain_items=n_unc, uncertain_usd=c_unc)
                return self.wait("approve_budget", "uncertain")
            else:
                retry_unc = True
        out_dir = screen._default_out_dir(self.cfg, self.job["idea"], store_now())
        with self.mu:
            self.job["approval"].setdefault("out_dirs", []).append(str(out_dir))
            self.save()

        def on_progress(phase: str, done: int, total: int) -> None:
            key = {"l1": "p_l1", "fetch": "p_fetch", "l2": "p_l2"}.get(phase)
            if key is None:
                return
            self.progress(4, T["zh"][key].format(done=f"{done:,}", total=f"{total:,}", s=f"{FETCH_DOCS_S:g}"),
                          T["en"][key].format(done=f"{done:,}", total=f"{total:,}", s=f"{FETCH_DOCS_S:g}"), done,
                          total)

        self.progress(4, T["zh"]["p_l1"].format(done=0, total="…"), T["en"]["p_l1"].format(done=0, total="…"),
                      force=True)
        try:
            res = screen.screen(self.cfg, self.job["idea"], idea_en=self.job["idea_en"],
                                min_mcap_usd=float(self.job["min_mcap_usd"]), countries=self.job.get("countries"),
                                budget_usd=rem, reads=READS, sieve="auto", out_dir=out_dir,
                                retry_uncertain=retry_unc, progress=on_progress,
                                jev_factory=self.d.jev_factory, keywords_fn=self.d.keywords_fn)
        except ValueError as e:
            return self.fail("failed", str(e)[:300], reason="screen_args")
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:  # noqa: BLE001
            if screen._is_error(e, "JevBusy"):
                return self.fail("busy", "another process is using Jev")
            if screen._is_error(e, "JevUnavailable"):
                return self.fail("ai_unavailable", str(e)[:200], http_status=_http_of(str(e)))
            raise
        self.add_run(res.get("run_id"))
        with self.mu:
            self.job["approval"].setdefault("costs", {})[res["run_id"]] = res.get("cost_usd")
            self.job["uncertain_ok"] = False
        with contextlib.suppress(Exception):   # refresh the views' stand-in; the next strict read decides anyway
            self.remaining()
        st = res["status"]
        self.record("screen", "ok" if st in ("ok", "partial", "budget_exhausted") else "failed", t0,
                    f"运行 {res['run_id']}：{st}", f"run {res['run_id']}: {st}", run_id=res["run_id"],
                    cost_usd=res.get("cost_usd"))
        if st == screen.STATUS_UNAVAILABLE:
            errs = " ".join(res.get("errors") or [])
            with self.mu:
                self.job["canary"] = None
            return self.fail("ai_unavailable", errs[:200], http_status=_http_of(errs))
        if st == screen.STATUS_BUSY:
            return self.fail("busy", "another process is using Jev")
        self.result = res
        if st == screen.STATUS_BUDGET:
            with self.mu:
                self.job["topup_usd"] = max(0.05, round(float(res.get("cost_usd") or 0.1), 2))
        return "ok"

    def load_result(self) -> dict[str, Any] | str:
        """The screen's result: this worker's, else (a resumed job) the run's folder by the screen step's run id
        (after an on-demand update the folder holds the update run). 'stop' after a failure record."""
        from . import cli, store
        res = getattr(self, "result", None)
        if res is not None:
            return res
        run_id = (step_of(self.job, "screen") or {}).get("run_id")
        try:
            with store.session(self.cfg, read_only=True, wait_s=60.0) as con:
                _rid, out_dir, res = cli._resolve_run(con, run_id)
        except (ValueError, store.StoreLocked) as e:
            return self.fail("failed", f"results not readable: {e}", reason="results")
        self.result = dict(res, output_dir=res.get("output_dir") or str(out_dir))
        return self.result

    def step_fetch(self, sid: str = "fetch") -> str:
        """After an ok / partial screen: the on-demand annual reports of its profile-only layer-2 companies and the
        update pass of the same report (ondemand_cli.run_fetch, the path `screen --fetch-docs auto` takes; at most
        FETCH_DOCS_S seconds, the update at most min($0.05, the approval's rest)). A fetch problem is a note and the
        step is 'skipped', never a failed job; SIGTERM (KeyboardInterrupt) propagates (the run's files are kept and
        a resumed worker fetches again: stored reports are then read by the update)."""
        from . import ondemand
        t0 = time.monotonic()
        T = STRINGS
        res = self.load_result()
        if res == "stop":
            return "stop"
        l2 = (res.get("layers") or {}).get("l2") or {}
        if res.get("status") not in ("ok", "partial") or not l2.get("profile_inputs") or res.get("supersedes"):
            # nothing to fetch (not finished / every layer-2 company read an annual report / already updated)
            self.record(sid, "skipped", t0, "不需要补抓年报", "no annual-report fetch needed")
            return "ok"
        try:
            left = self.remaining()
        except store_locked() as e:
            self.note(f"on-demand annual reports skipped: the database is busy ({str(e)[:120]})")
            self.record(sid, "skipped", t0, "数据库忙，没有补抓年报", "database busy; no fetch")
            return "ok"

        def on_progress(done: int, total: int) -> None:
            self.progress(4, T["zh"]["p_fetch"].format(done=f"{done:,}", total=f"{total:,}", s=f"{FETCH_DOCS_S:g}"),
                          T["en"]["p_fetch"].format(done=f"{done:,}", total=f"{total:,}", s=f"{FETCH_DOCS_S:g}"),
                          done, total)
        try:        # its console lines go to the worker's log
            ret = (self.d.fetch_docs or _default_fetch_docs)(
                self.cfg, res, time_s=FETCH_DOCS_S, update_budget=round(min(ondemand.UPDATE_BUDGET_DEFAULT, left), 6),
                on_progress=on_progress, out=print, jev_factory=self.d.jev_factory)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:  # noqa: BLE001 - the first report stands; the page says profile-only
            self.note(f"on-demand annual reports skipped: {type(e).__name__}: {str(e)[:200]}")
            self.record(sid, "skipped", t0, "补抓年报出错，保留第一版结果", "fetch failed; first report kept",
                        error=f"{type(e).__name__}: {str(e)[:200]}")
            return "ok"
        upd = (ret or {}).get("update") or {}
        self.add_run(upd.get("run_id"))
        if upd.get("run_id"):
            with self.mu:       # the update pass is paid (cache hits mostly): it counts against the approval
                self.job["approval"].setdefault("costs", {})[upd["run_id"]] = upd.get("cost_usd")
            with contextlib.suppress(Exception):
                self.remaining()
        final = dict((ret or {}).get("result") or res)
        final.setdefault("output_dir", res["output_dir"])
        if (ret or {}).get("fetch") and not (final.get("layers") or {}).get("fetch"):
            final["layers"] = {**(final.get("layers") or {}), "fetch": ret["fetch"]}   # run_fetch records it too
        self.result = final
        fetch = _fetch_brief((ret or {}).get("fetch")) or {}
        self.record(sid, "ok", t0, fetch.get("summary_zh") or "", fetch.get("summary_en") or "",
                    update_run_id=upd.get("run_id"), cost_usd=upd.get("cost_usd"), fetched=fetch.get("fetched"))
        return "ok"

    # -- the optional profile fill (asked once after the first result; see fill_offer / fill_item)
    def fill_offer(self, res: dict[str, Any]) -> dict[str, Any] | None:
        """The optional profile-fill question, when the idea's own market has a large hole: >= FILL_MIN_SHARE of its
        companies at the run's floor have no profile at all (the AI never read them), or >= FILL_MIN_TOP of its
        FILL_TOP largest do. Only for a market the idea points at (its countries, or Chinese for a Chinese idea),
        only when the fill can run (consent yes, no crawl cooldown) and its re-rank fits the approval's rest."""
        from . import ops, screen, store
        market = fill_market(self.job)
        if market is None or not consent_current(consent_state(self.cfg)):
            return None
        if ops.cooldown(self.cfg, "crawl-descriptions") is not None:
            return None
        floor = float(self.job["min_mcap_usd"])
        with store.session(self.cfg, read_only=True, wait_s=30.0) as con:
            rows = con.execute(
                """SELECT u.security_id, u.name, u.country, u.exchange, u.market_cap_usd, EXISTS (
                       SELECT 1 FROM descriptions d WHERE (d.company_key = u.company_key OR d.security_id =
                       u.security_id) AND d.text IS NOT NULL AND trim(d.text) <> '') AS described,
                       u.sector, u.industry
                   FROM universe u WHERE u.market_cap_usd >= ? ORDER BY u.market_cap_usd DESC, u.security_id""",
                [floor]).fetchall()
            keep = screen.country_matcher([market], {r[2] for r in rows if r[2]})
            mk = [r for r in rows if keep(r[2], r[3])]
            missing = [r for r in mk if not r[5]]
            if not mk or not missing:
                return None
            named, basis = fill_examples(con, res.get("run_id"), missing)
        share = len(missing) / len(mk)
        top_missing = sum(1 for r in mk[:FILL_TOP] if not r[5])
        if share < FILL_MIN_SHARE and top_missing < FILL_MIN_TOP:
            return None
        try:
            rem = self.remaining()
        except store_locked():
            return None
        est = max(0.01, round(len(missing) * FILL_ITEM_USD, 2))
        if est > rem + 1e-9:
            self.note(f"profile fill not offered: its re-rank (about ${est:.2f}) is above the approval's rest "
                      f"(${rem:.2f})")
            return None
        from . import page
        minutes = max(1, math.ceil(len(missing) / page.CRAWL_REQ_PER_S / 60))
        return {"country": market, "missing": len(missing), "companies": len(mk), "share": round(share, 3),
                "top_missing": top_missing, "minutes": minutes, "estimate_usd": est, "floor_usd": floor,
                "biggest": named, "names_basis": basis}

    def latest_run_id(self) -> str | None:
        """The newest run of the idea: the stable page's (card answers ...) when newer, else the job's result."""
        view = newest_view(self.cfg, self.job)
        return ((view.get("result") or {}).get("run_id")) or ((self.job.get("result") or {}).get("run_id"))

    def run_fill(self) -> None:
        """After a yes to the fill question: crawl the missing profiles of that market (the polite
        crawl-descriptions path under its own consent, cooldown and rate budget), then screen --from-run the newest
        run with l1_new (only the newly described companies are read by L1; everyone else keeps its answers for
        $0), fetch the annual reports of new profile-only L2 companies, re-rank and update the one page. A block or
        an empty fill keeps the current result."""
        from . import cli, ops, screen, store
        from .sources import tradingview_profiles
        fill = self.job["fill"]
        o = fill.get("offer") or self.job.get("fill_offer") or {}
        market = o.get("country") or "CN"
        mz, me = FILL_MARKET_WORDS.get(market, (market, market))
        t0 = time.monotonic()
        self.progress(2, STRINGS["zh"]["fill_running"].format(market=mz, n=o.get("missing", "?"),
                                                                m=o.get("minutes", "?")),
                      STRINGS["en"]["fill_running"].format(market=me, n=o.get("missing", "?"),
                                                           m=o.get("minutes", "?")), force=True)
        before = self.described_in(market)
        client = (self.d.crawl_client or cli.make_crawl_client)(self.cfg)
        crawl = self.d.crawl_descriptions or tradingview_profiles.crawl
        floor = float(self.job["min_mcap_usd"])
        code, summ = ops.run_networked(
            "crawl-descriptions", self.cfg,
            lambda: crawl(self.cfg, client, countries=[market], min_mcap_usd=floor), client=client,
            consent_source="tradingview_profile")
        added = max(0, self.described_in(market) - before)
        with self.mu:
            fill.update(crawl_status=summ.get("status"), crawl_exit=code, added=added,
                        crawl_seconds=round(time.monotonic() - t0, 1))
        if code == 2:
            ra = summ.get("retry_after") or "24 h"
            self.note(f"profile fill: TradingView refused ({summ.get('status')}); retry after {ra}")
            fill["message_zh"] = STRINGS["zh"]["fill_blocked"].format(retry_after=ra)
            fill["message_en"] = STRINGS["en"]["fill_blocked"].format(retry_after=ra)
        if not added:
            self.progress(5, STRINGS["zh"]["fill_none"], STRINGS["en"]["fill_none"], force=True)
            with self.mu:
                fill["finished_at"] = iso(now_utc())
                self.job["state"] = "done"
            return
        self.progress(2, STRINGS["zh"]["fill_ok"].format(k=added, s=int(time.monotonic() - t0)),
                      STRINGS["en"]["fill_ok"].format(k=added, s=int(time.monotonic() - t0)), force=True)
        try:
            rem = self.remaining()
        except store_locked() as e:
            self.note(f"profile fill: re-rank skipped, the database is busy ({str(e)[:120]})")
            self.fill_ended(fill, "fill_rerank_busy", k=added)
            return
        base = self.latest_run_id()
        out_dir = screen._default_out_dir(self.cfg, self.job["idea"], store_now())
        with self.mu:
            self.job["approval"].setdefault("out_dirs", []).append(str(out_dir))
            self.save()
        T = STRINGS
        self.progress(4, T["zh"]["fill_screen"].format(n=added), T["en"]["fill_screen"].format(n=added), force=True)

        def on_progress(phase: str, done: int, total: int) -> None:
            key = {"l1": "p_l1", "l2": "p_l2"}.get(phase)
            if key:
                self.progress(4, T["zh"][key].format(done=f"{done:,}", total=f"{total:,}"),
                              T["en"][key].format(done=f"{done:,}", total=f"{total:,}"), done, total)
        t1 = time.monotonic()
        try:
            res = screen.screen(self.cfg, self.job["idea"], idea_en=self.job["idea_en"], from_run=base, l1_new=True,
                                budget_usd=rem, reads=READS, sieve="auto", out_dir=out_dir, progress=on_progress,
                                jev_factory=self.d.jev_factory, keywords_fn=self.d.keywords_fn)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:  # noqa: BLE001 - the current result stands
            self.note(f"profile fill: re-rank failed ({type(e).__name__}: {str(e)[:160]}); the result is unchanged")
            self.fill_ended(fill, "fill_rerank_failed", k=added)
            return
        self.add_run(res.get("run_id"))
        with self.mu:
            self.job["approval"].setdefault("costs", {})[res["run_id"]] = res.get("cost_usd")
            fill.update(run_id=res["run_id"], screen_status=res["status"], screen_seconds=round(
                time.monotonic() - t1, 1))
        if res["status"] not in ("ok", "partial"):
            self.note(f"profile fill: re-rank ended {res['status']}; the result is unchanged")
            self.fill_ended(fill, "fill_rerank_budget" if res["status"] == "budget_exhausted"
                            else "fill_rerank_failed", k=added, c=_usd(float(res.get("cost_usd") or 0.0)))
            return
        self.result = res
        self.step_fetch("fill_fetch")
        with self.mu:
            fill["finished_at"] = iso(now_utc())
        self.step_finish()

    def fill_ended(self, fill: dict[str, Any], key: str, **kw: Any) -> None:
        """The fill ends without a new version: the human is told what came in and why the result is unchanged."""
        with self.mu:
            fill["message_zh"] = STRINGS["zh"][key].format(**kw)
            fill["message_en"] = STRINGS["en"][key].format(**kw)
            fill["finished_at"] = iso(now_utc())
            self.job["state"] = "done"

    def described_in(self, market: str) -> int:
        from . import screen, store
        with store.session(self.cfg, read_only=True, wait_s=WORKER_DB_WAIT_S) as con:
            rows = con.execute(
                """SELECT u.country, u.exchange FROM universe u WHERE u.market_cap_usd >= ? AND EXISTS (
                       SELECT 1 FROM descriptions d WHERE (d.company_key = u.company_key OR d.security_id =
                       u.security_id) AND d.text IS NOT NULL AND trim(d.text) <> '')""",
                [float(self.job["min_mcap_usd"])]).fetchall()
        keep = screen.country_matcher([market], {r[0] for r in rows if r[0]})
        return sum(1 for r in rows if keep(r[0], r[1]))

    def step_finish(self) -> str:
        from . import cli, page
        t0 = time.monotonic()
        T = STRINGS
        self.progress(5, T["zh"]["p_finish"], T["en"]["p_finish"], force=True)
        res = self.load_result()
        if res == "stop":
            return "stop"
        # an on-demand update replaced the screen's files: the cost and time are the screen's plus the update's
        costs = (self.job.get("approval") or {}).get("costs") or {}
        cost = float(res.get("cost_usd") or 0.0) + (float(costs.get(res["supersedes"]) or 0.0)
                                                    if res.get("supersedes") else 0.0)
        cost = round(cost, 6) if res.get("cost_usd") is not None else None
        secs = [(step_of(self.job, k) or {}).get("seconds") for k in ("screen", "fetch")]
        seconds = (round(sum(x for x in secs if x is not None), 1) if res.get("supersedes") and secs[0] is not None
                   else (res.get("timing") or {}).get("total_s"))
        deck, _path = cli._write_run_cards(self.cfg, res, CARDS)
        self.tick()
        totals = idea_totals(self.cfg, self.job["idea"], self.job, wait_s=30.0)
        path, data = page.write_page(self.cfg, res["output_dir"], res, deck, lang=self.job.get("lang") or "zh",
                                     extra={"totals": totals} if totals else None)
        stable = page.stable_path(self.cfg, self.job["idea"]) if path and page.stable_holds(
            self.cfg, self.job["idea"], res["run_id"]) else path
        opened = False
        if stable is not None and not self.job.get("no_open") and res["run_id"] not in self.job["opened_run_ids"]:
            opened = bool((self.d.open_page or _default_open)(stable))
            if opened:
                with self.mu:
                    self.job["opened_run_ids"].append(res["run_id"])
        with self.mu:
            self.job["notes"] = [n for n in self.job.get("notes") or []      # resolved by this result
                                 if not str(n).startswith(("worker stopped without finishing", "interrupted ("))]
            self.job["result"] = result_block(self.cfg, self.job, res, deck, path, data, stable=stable,
                                              opened=opened, cost=cost, seconds=seconds)
            if totals:
                self.job["totals"] = totals
            self.job["state"] = "done"
            self.job["waiting_on"] = None
        if not (self.job.get("fill") or {}).get("answer"):
            try:
                offer = self.fill_offer(res)
            except Exception as e:  # noqa: BLE001 - the optional question is never worth a failed job
                offer = None
                self.note(f"profile-fill question skipped: {type(e).__name__}: {str(e)[:120]}")
            with self.mu:
                self.job["fill_offer"] = offer
        self.record("finish", "ok", t0, "结果页已生成" if path else "结果页没有生成", "page written" if path
                    else "page not written", page=str(stable) if stable else None)
        return "ok"

    def run(self) -> int:
        fill = self.job.get("fill") or {}
        if fill.get("answer") == "yes" and not fill.get("finished_at") and self.job.get("result") \
                and step_done(self.job, "finish"):
            self.run_fill()              # the first result stands; only the fill and its re-rank run
            return 0
        order = [("check", self.step_check), ("universe", self.step_universe),
                 ("descriptions", self.step_descriptions), ("pack", self.step_pack),
                 ("idea_en", self.step_idea_en), ("ai_check", self.step_ai_check),
                 ("estimate", self.step_estimate), ("screen", self.step_screen), ("fetch", self.step_fetch),
                 ("finish", self.step_finish)]
        for sid, fn in order:
            self.merge_inbox()
            if self.job.get("state") in ("failed", "waiting", "done", "declined"):
                break
            if sid != "finish" and step_done(self.job, sid):
                continue                 # done by an earlier worker (resume); finish reloads the run by its id
            with self.mu:
                self.job["current_step"] = sid
            if fn() == "stop":
                break
            if sid == "universe":
                self.after_universe()
        return 0


def _next_steps(gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    steps = [{"text_zh": STRINGS["zh"]["next_idea"], "text_en": STRINGS["en"]["next_idea"], "command": None,
              "cost_usd": 0.25, "minutes": 4},
             {"text_zh": STRINGS["zh"]["next_cards"], "text_en": STRINGS["en"]["next_cards"], "command": None,
              "cost_usd": 0.01, "minutes": 3}]
    g = next((x for x in gaps if x["id"] == "no_description" and x.get("cn")), None)
    if g is not None:
        steps.append({"text_zh": STRINGS["zh"]["next_gap_cn"].format(n=g["cn"], m=g["minutes"]),
                      "text_en": STRINGS["en"]["next_gap_cn"].format(n=g["cn"], m=g["minutes"]),
                      "command": g["command"], "cost_usd": 0.0, "minutes": g["minutes"]})
    elif any(x["id"] == "us_profile_only" for x in gaps):
        steps.append({"text_zh": STRINGS["zh"]["next_gap_sec"], "text_en": STRINGS["en"]["next_gap_sec"],
                      "command": "jevscreen keys set sec-email", "cost_usd": 0.0, "minutes": 2})
    elif gaps:
        steps.append({"text_zh": gaps[0]["text_zh"], "text_en": gaps[0]["text_en"],
                      "command": gaps[0].get("command"), "cost_usd": 0.0, "minutes": None})
    return steps[:3]


def result_block(cfg, job: dict[str, Any], res: dict[str, Any], deck: dict[str, Any] | None, path: Path | None,
                 data: dict[str, Any] | None, *, stable: Path | None, opened: bool, cost: float | None,
                 seconds: float | None) -> dict[str, Any]:
    """job['result'] of a finished run: the page, deck, summary, top rows, next steps and the on-demand fetch."""
    from . import page
    sv = _sieve(cfg, job["idea"])
    gaps = (data or {}).get("gaps") or []
    top = page.top_rows(data, 10)
    change = (data or {}).get("change") or page.change_of(res) or {}
    return {"run_id": res["run_id"], "status": res["status"], "output_dir": res["output_dir"],
            "started_at": res.get("started_at"),
            "page": str(stable) if stable else None, "run_page": str(path) if path else None,
            "page_opened": opened, "deck_id": (deck or {}).get("deck_id"), "cost_usd": cost,
            "seconds": seconds, "summary": page.summary_of(data, deck),
            "top": top, "next_steps": _next_steps(gaps), "gaps": gaps,
            "version": (data or {}).get("version") or 1, "change_zh": change.get("zh"), "change_en": change.get("en"),
            "sieve_version": (sv or {}).get("version"), "idea_en": job.get("idea_en"),
            "min_mcap_usd": job.get("min_mcap_usd"), "countries": job.get("countries"),
            "fetch": _retarget_fetch(_fetch_brief((res.get("layers") or {}).get("fetch")), res["run_id"], top),
            "translation": _translation_of(cfg, res["run_id"], data)}


def _translation_of(cfg, run_id: str, data: dict[str, Any] | None) -> dict[str, Any] | None:
    from . import quickstart_cli
    return quickstart_cli.translation_block(cfg, run_id, data)


def follow_update(cfg, replaced_run_id: str, res: dict[str, Any], deck: dict[str, Any] | None,
                  path: Path | None, data: dict[str, Any] | None) -> bool:
    """`fetch-docs` replaced a run with its update: a finished quickstart job of that run now reports the update
    (run, page, deck_id, top rows, the fetch outcome; cost = the job's + the update's). Other jobs are left alone.
    Returns whether a job was changed."""
    from . import guard, page
    key = idea_key((res.get("idea") or "").strip()) if (res.get("idea") or "").strip() else None
    if key is None or load_job(cfg, key) is None:
        return False
    try:
        lock = guard.budget_lock(cfg, LOCK)
        lock.__enter__()
    except guard.Busy:
        return False          # a worker runs: it reloads the run by its folder when it finishes
    try:
        job = load_job(cfg, key)
        old = (job or {}).get("result") or {}
        if not job or job.get("state") != "done" or old.get("run_id") != replaced_run_id:
            return False
        if data is None:      # no page was written for the run folder: rebuild the page data without writing
            data = page.build_page_data(res, deck, lang=job.get("lang") or "zh")
        stable = page.stable_path(cfg, job["idea"]) if path and page.stable_holds(cfg, job["idea"], res["run_id"]) \
            else (path or (Path(old["page"]) if old.get("page") else None))
        upd = float(res.get("cost_usd") or 0.0)
        cost = None if old.get("cost_usd") is None else round(float(old["cost_usd"]) + upd, 6)
        job["result"] = result_block(cfg, job, res, deck, path or (Path(old["run_page"]) if old.get("run_page")
                                                                   else None), data,
                                     stable=stable, opened=bool(old.get("page_opened")), cost=cost,
                                     seconds=old.get("seconds"))
        job["result"]["sieve_version"] = old.get("sieve_version")    # the same sieve screened it (reuse check)
        save_job(cfg, job)
        return True
    finally:
        lock.__exit__(None, None, None)


# ------------------------------------------------------------------------------------------------ newest run, totals

_PAGE_DATA = re.compile(r'<script type="application/json" id="data">(.*?)</script>', re.S)


def read_page_data(path: str | Path) -> dict[str, Any] | None:
    """The data block of a result page (exactly what the page shows), or None."""
    try:
        m = _PAGE_DATA.search(Path(path).read_text(encoding="utf-8"))
        data = json.loads(m.group(1)) if m else None
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _page_view(cfg, data: dict[str, Any]) -> dict[str, Any]:
    """What a finished job reads from its page every time: the top rows (with the agent's translations once they
    are imported) and the translation step still to do (quickstart_cli.translation_block)."""
    from . import page, quickstart_cli
    return {"top": page.top_rows(data, 10),
            "translation": quickstart_cli.translation_block(cfg, data.get("run_id"), data)}


def newest_view(cfg, job: dict[str, Any]) -> dict[str, Any]:
    """The job as the human should see it: its result always points at the one stable page of the idea
    (<home>/pages/<idea_key>.html), and when a newer run of the idea owns that page (the human's card answers,
    fetch-docs, a plain screen) the run id, top rows, summary, deck and gaps come from that page. Never writes."""
    from . import page
    res = job.get("result") or {}
    if not res.get("run_id"):
        return job
    sp = page.stable_path(cfg, job["idea"])
    owner = page.stable_run(cfg, job["idea"])
    if owner is None or not sp.exists():
        return job
    if owner.get("run_id") == res.get("run_id"):
        data = read_page_data(sp)          # the page may have changed since (the agent's translations)
        fresh = _page_view(cfg, data) if data and data.get("run_id") == res.get("run_id") else {}
        return {**job, "result": {**res, "page": str(sp), **fresh}}
    if res.get("started_at") and owner.get("started_at") and str(owner["started_at"]) < str(res["started_at"]):
        return job                   # this run's page could not be written: the stable page is older, not newer
    data = read_page_data(sp)
    if not data or data.get("run_id") != owner.get("run_id"):
        return {**job, "result": {**res, "page": str(sp)}}
    gaps = data.get("gaps") or []
    new = {**res, "run_id": data["run_id"], "page": str(sp), "run_page": None, "deck_id": data.get("deck_id"),
           "status": data.get("status") if data.get("status") in ("ok", "partial", "budget_exhausted")
           else res.get("status"), "summary": page.summary_of(data, {"cards": data.get("cards") or []}),
           "top": page.top_rows(data, 10), "gaps": gaps, "next_steps": _next_steps(gaps),
           "version": data.get("version"), "change_zh": (data.get("change") or {}).get("zh"),
           "change_en": (data.get("change") or {}).get("en"), "newer_than_job": True,
           "cost_usd": data.get("cost_usd"), "seconds": data.get("seconds"), **_page_view(cfg, data)}
    if res.get("fetch"):
        new["fetch"] = _retarget_fetch(res["fetch"], data["run_id"], page.top_rows(data, 10))
    return {**job, "result": new}


def idea_totals(cfg, idea: str, job: dict[str, Any] | None = None, *,
                wait_s: float = VIEW_DB_WAIT_S) -> dict[str, Any] | None:
    """{'cost_usd', 'seconds'} of the idea so far: every Jev cost of its screen runs since its quickstart job
    started (the ledger) plus the key test, and the time the background work took (downloads, AI, fetches: the
    worker's own clock, not the time spent waiting for answers) plus the runs made outside it (card answers).
    Without a job: all the idea's runs. A locked store gives the worker's last reading (job['totals'])."""
    from . import store
    job = load_job(cfg, idea_key(idea)) if job is None else job
    if not Path(cfg.db_path).exists():
        return (job or {}).get("totals")
    created = parse_iso((job or {}).get("created_at"))
    mine = set((job or {}).get("run_ids") or [])
    try:
        with store.session(cfg, read_only=True, wait_s=wait_s, poll_s=VIEW_DB_POLL_S) as con:
            runs = con.execute(
                "SELECT r.run_id, r.started_at, r.finished_at, coalesce((SELECT sum(q.cost_usd) FROM jev_requests q "
                "WHERE q.run_id = r.run_id), 0) FROM screen_runs r WHERE trim(r.idea) = ?",
                [idea.strip()]).fetchall()
    except Exception:  # noqa: BLE001 - locked: the last reading
        return (job or {}).get("totals")
    runs = [r for r in runs if created is None or r[1] is None or r[1] >= created]
    cost = sum(float(r[3] or 0.0) for r in runs) + float(((job or {}).get("approval") or {}).get("canary_usd")
                                                             or 0.0)
    outside = sum((r[2] - r[1]).total_seconds() for r in runs
                  if r[0] not in mine and r[1] is not None and r[2] is not None)
    secs = float((job or {}).get("worker_seconds") or 0.0) + outside if job else outside
    return {"cost_usd": round(cost, 6), "seconds": round(secs, 1)}


def _retarget_fetch(fetch: dict[str, Any] | None, run_id: str | None,
                    top: list[dict[str, Any]]) -> dict[str, Any] | None:
    """fetch.questions for the human: each then_command names the run the page shows now (not the run the fetch
    started from), and the optional second-round questions come only when a company they help is in the top 10:
    sec_email for a US company, mops_annual for a Taiwan one (opendart for a Korean one), labelled optional."""
    if not fetch:
        return fetch
    countries = {(r.get("country") or "") for r in top or []}
    need = {"sec_email": "United States" in countries, "mops_annual": "Taiwan" in countries,
            "opendart": "South Korea" in countries}
    qs_out = []
    for q in fetch.get("questions") or []:
        if not need.get(q.get("id"), True):
            continue
        q = dict(q)
        if run_id and q.get("then_command"):
            q["then_command"] = f"jevscreen fetch-docs {run_id}"
        q["optional"] = True
        for lang in ("zh", "en"):
            k = f"human_question_{lang}"
            if q.get(k) and STRINGS[lang]["optional_q"].strip() not in q[k]:
                q[k] = q[k] + STRINGS[lang]["optional_q"]
        qs_out.append(q)
    return {**fetch, "questions": qs_out}


def _fetch_brief(fetch: dict[str, Any] | None) -> dict[str, Any] | None:
    """The job result's view of layers.fetch: status, companies fetched per source, seconds, the update's outcome,
    the next command and the human questions (sec_email / opendart / mops consent)."""
    if not fetch:
        return None
    upd = fetch.get("update") or {}
    return {"status": fetch.get("status"), "fetched": fetch.get("fetched") or {}, "seconds": fetch.get("seconds"),
            "update": {k: upd.get(k) for k in ("run_id", "status", "cost_usd", "skipped") if upd.get(k) is not None},
            "next_command": fetch.get("next_command"), "questions": fetch.get("questions") or [],
            "summary_zh": fetch.get("summary_zh"), "summary_en": fetch.get("summary_en")}


def store_locked() -> type:
    from . import store
    return store.StoreLocked


def store_now() -> dt.datetime:
    from . import store
    return store.now_utc()


def _http_of(text: str) -> int | None:
    m = re.search(r"HTTP (40[123])", text or "")
    return int(m.group(1)) if m else None


COUNTRY_WORDS: dict[str, tuple[str, ...]] = {
    "CN": ("中国", "A股", "China", "Chinese"), "US": ("美国", "美股", "United States", "US ", "American"),
    "JP": ("日本", "Japan", "Japanese"), "KR": ("韩国", "Korea", "Korean"), "TW": ("台湾", "Taiwan"),
    "HK": ("香港", "Hong Kong"), "IN": ("印度", "India", "Indian"), "DE": ("德国", "Germany", "German"),
}


def worker(cfg, key: str, deps: Deps | None = None) -> int:
    """The detached worker (see the module docstring). Returns 0 (the job file carries the outcome)."""
    from . import cli, guard
    deps = deps or DEPS
    lock = None
    deadline = time.monotonic() + 5.0          # the spawning front may still hold the lock for a moment
    while lock is None:
        try:
            lk = guard.budget_lock(cfg, LOCK)
            lk.__enter__()
            lock = lk
        except guard.Busy:
            if time.monotonic() >= deadline:
                return 0                       # another worker has it
            time.sleep(0.2)
    stop = None
    w = None
    try:
        job = load_job(cfg, key)
        if job is None:
            return 1
        w = Worker(cfg, job, deps)
        with w.mu:
            job["state"] = "running"
            t = iso(now_utc())
            job["worker"] = {"pid": os.getpid(), "started_at": t, "heartbeat_at": t}
            job["failure"] = None if job.get("failure") and job["failure"].get("kind") != "blocked" else \
                job.get("failure")
            job["waiting_on"] = None
            job.setdefault("work_started_at", t)
            w.save()
        stop = threading.Thread(target=w.heartbeat_loop, daemon=True)
        stop.start()
        try:
            with cli.sigterm_as_interrupt():
                w.run()
        except KeyboardInterrupt as e:
            with w.mu:
                job["state"] = "interrupted"
                job["notes"].append(f"interrupted ({type(e).__name__}) at {iso(now_utc())}")
                w.save()
        except JobSecret:
            raise
        except Exception as e:  # noqa: BLE001 - recorded in the job; the log has the traceback
            import traceback
            traceback.print_exc()
            with w.mu:
                job["state"] = "failed"
                job["failure"] = {"kind": "failed", "error": f"{type(e).__name__}: {str(e)[:300]}",
                                  "at": iso(now_utc())}
                w.save()
        with w.mu:
            w.tick()
            if job.get("state") == "running":      # ran out of steps without a verdict (should not happen)
                job["state"] = "waiting" if job.get("waiting_on") else "failed"
                if job["state"] == "failed" and not job.get("failure"):
                    job["failure"] = {"kind": "failed", "error": "stopped without a result", "at": iso(now_utc())}
            w.save()
        return 0
    finally:
        if w is not None:
            w.stop.set()
        lock.__exit__(None, None, None)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) == 2 and argv[0] == "--worker":
        from . import config
        return worker(config.load(), argv[1])
    print("usage: python -m jevscreen.quickstart --worker IDEA_KEY (started by `jevscreen quickstart`)",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
