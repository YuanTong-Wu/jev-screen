"""The ONE page per idea (<home>/pages/<idea_key>.html; also page.html in each run folder) that a non-technical human
opens in a browser: prerequisites checklist and live progress (data['live'], jevscreen.pagestatus), a slot for scope
questions (data['questions'], hidden while empty), then the results (top-10 table, one expandable row per company).
With JavaScript, that plain page is the "text view" behind a full-window 3D sand scene with a restrained HUD
(jevscreen.page_sand, jevscreen.page_hud: the checklist behind one status dot, the progress on the sieves' rims and
a tiny instrument, the results and questions in a side panel / bottom sheet); a toggle, `#text` and printing show it.

Rules (see docs/DATA_RULES.md "Result page"):
- Self-contained, no network: a strict Content-Security-Policy (default-src 'none'; inline style and script only;
  images only as data: URIs; no base, no form action), referrer off, system fonts with CJK fallbacks. No external
  link, stylesheet, import or font is ever referenced.
- Untrusted text (company names, descriptions, filing quotes) sits in one <script type="application/json"> block
  (_json_for_script escapes <, >, &, U+2028, U+2029) and is rendered with textContent only. Only http(s) URLs become
  links (_safe_url), with rel="noopener noreferrer".
- Deterministic for the same inputs (sorted JSON, no clock), under 300 KB: at most MAX_ROWS ranked rows,
  MAX_UNVERIFIED unverified rows and MAX_CARDS cards. No absolute home path appears (the footer shows run id,
  deck id and version).
- ONE language per page (--lang, default the idea's quickstart language): a zh page is fully Chinese, an en page
  fully English (jevscreen.l10n: sources, forms, dates, money, countries); no toggle. Texts the data holds in
  another language show the user's agent's translation (jevscreen.translations) with an 'AI translation' tag and
  the verbatim original behind a 'show original' button; without a translation the original, marked.
- Fact / inference / gap / your call are labelled separately: the filing quote is a fact (with its link), the
  verdict is the AI's inference, missing text is a gap, card answers are the user's call.
- No card buttons and no answer bar (owner decision 2026-09-27): cards are a CLI tool for the user's AI. The card data
  stays in the data block (its plain-word forms and translations serve the `cards` printout); it is not rendered.
- While work runs the page reloads itself every REFRESH_S seconds (data['live']['refresh']): a small JS timer
  (REFRESH_JS) that waits while someone is using the page (a pointer pressed, a key, the sand drawing hovered or
  dragged within the last ~4 s), with a <noscript> <meta http-equiv="refresh"> for a browser without JavaScript. The
  quickstart worker rewrites the page (pagestatus.write) and the page restores its open rows, opened originals, chosen scope answers and
  scroll position (sessionStorage). A worker heartbeat older than quickstart.HEARTBEAT_STALE_S (live['stale']) turns
  the page to 'the background work stopped' even though no one rewrites it any more.
- Before writing, the page is scanned for every configured secret (ops.scan_secrets); a hit means no page.
- A page failure is a warning, never an exit code.
"""
from __future__ import annotations

import contextlib
import copy
import datetime as dt
import json
import math
import os
import re
import shutil
import sys
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterable

from . import l10n

PAGE_FORMAT = "jevscreen.page/1"
MAX_ROWS = 40
MAX_UNVERIFIED = 60
MAX_CARDS = 8
MAX_PAGE_BYTES = 300_000
ONE_LINE_CHARS = 160
SHORT_LIST = 10          # a list under this many rows says so (quickstart.SHORT_LIST: the chat says the same)
QUOTE_CHARS = 300
CNY_PER_USD = 7.2            # fixed, labelled rate for the ≈ ¥ figure next to USD
CRAWL_REQ_PER_S = 1.96       # measured effective profile-crawl rate (minutes estimate of the China gap fill)
BADGE_ORDER = ("second_search", "no_mention", "profile_only", "stale", "edge", "read_once", "backfill", "scope_no_target",
               "scope_unchecked", "agent_thin", "profile_gap_report_target", "l1_rescued", "constraint_missing",
               "constraint_unclear", "constraint_unchecked", "judge_c_product", "judge_c_role", "judge_c_target",
               "judge_unchecked", "judge_not_read")
TOP_TABLE = 10
CSP = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; "
       "base-uri 'none'; form-action 'none'")
COUNTRY_ZH = l10n.COUNTRY_ZH       # kept for callers of page.COUNTRY_ZH
CN_EXCHANGES = ("SSE", "SZSE", "BJSE")

STRINGS: dict[str, dict[str, str]] = {
    "zh": {
        "title": "筛选结果", "idea": "你的想法", "idea_en": "发给 AI 的英文句子（点开看）",
        "idea_orig": "你原来写的", "ai_tr": "AI 翻译", "ai_tr_quote": "AI 翻译（不是原文）", "show_orig": "看原文", "hide_orig": "收起原文",
        "orig_label": "原文", "untranslated": "原文（未翻译）", "text_ai": "（AI 翻译）", "text_untranslated": "（原文，未翻译）",
        "legend_ai": "AI 翻译＝你的 AI 译的，点「看原文」核对；原文才是事实",
        "version_first": "第 1 版", "version_n": "第 {n} 版：应用了你的 {k} 个回答（上一版 {prev}）",
        "version_n0": "第 {n} 版（上一版 {prev}）",
        "meta": "{date} · {status} · 花费 ${cost}{cny} · 用时 {time}",
        "meta_total": "{date} · {status} · 这个想法累计花费 ${cost}{cny} · 累计用时 {time}（不算等你回答的时间）",
        "version_change": "第 {n} 版：{change}（上一版 {prev}）",
        "cny": "（约 ¥{y}，按 1 美元 ≈ 7.2 元）", "cny_tiny": "（不到 ¥0.01，按 1 美元 ≈ 7.2 元）",
        "status_ok": "完成", "status_partial": "部分完成", "status_budget_exhausted": "预算用完（部分结果）",
        "status_other": "未完成",
        "funnel": "从 {universe} 家公司（市值 ≥ {floor}）中，{described} 家有简介、被 AI 读过；{l1} 家初读通过，"
                  "{listed} 家经年报或简介核对后入选。",
        "funnel_rescued": "入选的公司里有 {n} 家初读没通过（简介太薄或偏题），是年报写明了才入选的。",
        "short_note": "名单只有 {n} 家，不到 10 家：原文明确写了这项业务的公司就这么多。下面「初读通过、但没有确认的」{u} 家"
                      "可以当作待确认名单看。",
        "short_note0": "名单只有 {n} 家，不到 10 家：原文明确写了这项业务的公司就这么多。可以换个说法，或者放宽范围再试。",
        "short_note_open": "名单只有 {n} 家，不到 10 家，但这不一定是全部：{why}，名单可能还会变多。",
        "short_why_unchecked": "下面「初读通过、但没有确认的」里有 {k} 家还没核对（预算用完、核对出错或没排上）",
        "short_why_run": "这次运行没有全部做完",
        "rank_note": "排序＝与你的想法吻合的程度（证据越直接越靠前，同等时按市值）。不看估值和财务，不是买入建议。",
        "banner": "仅供你个人研究：本页含只限个人使用的 TradingView / Yahoo 数据和年报原文摘录。请勿转发、截图公开或上传。",
        "legend_title": "怎么读这一页",
        "legend_fact": "事实＝年报原文，附链接", "legend_inference": "推断＝AI 的判断",
        "legend_gap": "缺口＝没拿到或没读到的", "legend_user": "你的判断＝你或你的 AI 回答校准问题时给的判断",
        "tag_fact": "事实", "tag_inference": "推断", "tag_gap": "缺口", "tag_user": "你的判断",
        "search": "按名字或代码查找…", "search_none": "这页没有它？让你的 AI 运行 jevscreen why <名字>，会告诉你原因",
        "filter_all": "全部", "filter_report": "有年报", "filter_edge": "边缘",
        "list_title": "入选名单（按原排名，{n} 家）", "list_empty": "这次没有公司通过核对。",
        # the confirmed list and the to-confirm section (never padded to 10: owner decision 2026-09-29)
        "funnel_split": "从 {universe} 家公司（市值 ≥ {floor}）中，{described} 家有简介、被 AI 读过；{l1} 家初读通过，"
                        "再经年报或简介核对。",
        "split_line": "确认 {m} 家；另有 {u} 家待核对。", "split_line0": "确认 {m} 家。",
        "split_open": "名单可能还会变：{why}。",
        "chat_top_more": "以上是确认名单的前 {n} 家；另有 {k} 家确认的公司没在这里列出，结果页列出全部 {m} 家。",
        "main_title": "确认的公司（{n} 家）", "main_rows_title": "确认的公司：逐家证据（{n} 家，点开看）",
        "main_note": "推断：只收初读判为核心、核对时 AI 认为原文明确写了这项业务的公司，「原文写明」是 AI 的判断；证据见每家，不凑满 10 家。",
        "main_note_judge": "推断：只收 AI 逐项读原文、认为是它自己的产品、它是供应方、想法的目标写到了的公司，「原文写明」是 AI 的判断；不凑满 10 家。",
        "main_note_agent": "另有 {n} 家原本在待核对里，是你的 AI 逐句读原文后确认的（标着「你的 AI 核对」）。",
        "main_note_agent_all": "推断：确认的 {n} 家原本都在待核对里，是你的 AI 逐句读原文后认为写明了这项业务（标着「你的 AI 核对」）；证据见每家，不凑满 10 家。",
        "main_empty": "这次没有公司能从原文确认。下面「待核对」有 {u} 家，可以先看那里。",
        "main_empty0": "这次没有公司能从原文确认。可以换个说法，或者放宽范围再试。",
        "main_empty_open": "这次还没有公司能从原文确认。",
        "confirm_title": "待核对（{n} 家，不算入选）",
        "confirm_note": "AI 觉得相关，但初读只算相关业务、原文没写明、摘录没提到你的想法、只有简介，或被移到了后面。请看原文自己判断，或让你的 AI 核对。",
        "funnel_rescued_split": "列出的公司里有 {n} 家初读没通过（简介太薄或偏题），是年报提到了才列出的。",
        "unverified_title_split": "初读通过、没有列出的（{n} 家）",
        "short_why_unchecked_split": "下面「初读通过、没有列出的」里有 {k} 家还没核对（预算用完、核对出错或没排上）",
        # the confirmed rows' word; the plain page says in main_note that it is the AI's reading, the HUD panel
        # (which hides main_note) says it once in main_hint
        "verdict_main": "原文写明", "main_hint": "推断：「原文写明」是 AI 的判断。",
        "via_agent": "你的 AI 核对", "via_agent_no": "你的 AI 认为不符，移到待核对",
        "badge_constraint_missing": "原文没写明想法限定的市场、地域或角色（降为相关）",
        "badge_constraint_unclear": "说不清是否符合想法限定的市场、地域或角色（降为相关）",
        "badge_constraint_unchecked": "没来得及核对想法限定的市场、地域或角色（预算或服务原因），请自己确认",
        "badge_judge_c_product": "推断：AI 读原文认为它卖的是部件、只装在自家总成里或相邻产品，不是想法里的产品（已排到后面，交你的 AI 复核）",
        "badge_judge_c_role": "推断：AI 读原文认为它是买方、上游、渠道、少数持股方或别的环节，不是供应方（已排到后面，交你的 AI 复核）",
        "badge_judge_c_target": "推断：AI 读原文认为它只服务别的市场、用途或地域（已排到后面，交你的 AI 复核）",
        "badge_judge_unchecked": "没来得及逐项核对（预算或服务原因），请自己确认",
        "badge_judge_not_read": "排在逐项核对的前 150 家之后，没有逐项核对，请自己确认",
        "badge_second_search": "推断：第一次读的年报段落没说清，二次检索找到同一份年报的另一段提到相关内容，最多算相关；原文段落是事实，请自己看",
        "verdict_explicit": "明确", "verdict_partial": "相关", "verdict_edge": "边缘",
        "evidence_annual_report": "年报原文", "evidence_profile": "只有简介",
        "evidence_user": "按你的判断（AI 没从原文确认）",
        "no_quote": "（没有可引用的原文）", "quote_from": "出处", "open_link": "打开原文",
        "details": "细节", "reads_label": "读取次数", "reads": "读了 {n} 次，{k} 次判为符合", "reads_one": "读了 1 次",
        "l1": "初读（简介）", "l2": "核对", "p_pos": "AI 判断符合的把握", "p_core": "初读核心分（调试）",
        "lbl_core": "核心", "lbl_adjacent": "相邻", "lbl_unrelated": "无关", "lbl_insufficient": "说不清",
        "lbl_explicit": "明确", "lbl_partial": "相关", "lbl_contradicted": "不符",
        "quote_gap": "年报摘录未提到（缺口）", "quote_gap_profile": "简介摘录未提到（缺口）",
        "quote_gap_why": "这段摘录里没有你的想法里的任何词（{terms}）。结论只来自 AI 的推断，请自己核实。",
        "quote_gap_why_user": "这段摘录里没有你的想法里的任何词（{terms}）。这一家是按你的判断列入的。",
        "more": "展开全文", "less": "收起",
        "top_title": "原排名前 {n} 家（表内按结论等级排列，# 为原排名）", "col_rank": "#", "col_name": "公司", "col_verdict": "结论", "col_evidence": "证据",
        "ev_short_annual_report": "年报", "ev_short_profile": "简介", "ev_short_gap": "年报没提到",
        "ev_short_gap_profile": "简介没提到",
        "unverified_group": "{n} 家：{why}",
        "mcap": "市值", "badges": "标记", "what": "做什么", "what_ar": "主营业务（年报）", "what_profile": "简介里写的",
        "badge_profile_only": "只有简介，没有年报（证据弱）", "badge_stale": "年报超过 3 年",
        "badge_edge": "多读几次可能变", "badge_read_once": "只读了一次", "badge_backfill": "递补，未经核对",
        "badge_no_mention": "摘录没提到你的想法", "badge_scope_no_target": "摘录没提到具体对象（按你的范围回答排后）",
        "badge_scope_unchecked": "范围回答未检查（预算不够）", "badge_agent_thin": "证据太少，你的 AI 也判断不了",
        "badge_user": "你的判断", "badge_unchecked": "未核对", "badge_l1_rescued": "简介里没写，年报里写了",
        "badge_profile_gap_report_target": "简介里没写，年报里写了",
        "cards_title": "帮系统判断几家（可选，约 3–5 分钟）",
        "card_rank": "现排第 {rank}", "card_not_listed": "未入选", "card_why": "为什么问",
        "card_what": "做什么", "card_quote": "原文", "card_explain": "看不懂摘录？把卡号告诉你的 AI，让它解释",
        "yes": "要", "no": "不要", "unsure": "不确定", "clear": "清除",
        "yes_more": "可选：哪一种？", "no_more": "可选：为什么不要？",
        "bar_label": "复制给你的 AI：", "copy": "复制", "copied": "已复制",
        "copy_failed": "复制失败：请手动选中上面的文字复制", "copy_prefix": "请运行：",
        "no_cards": "这次没有需要你判断的卡", "bar_empty": "（先在上面的卡片上点选）",
        "unverified_title": "初读通过、但没有确认的（{n} 家）",
        "st_contradicted": "年报/简介里没找到支持", "st_skipped_budget": "预算用完没核对", "st_failed": "核对出错",
        "st_no_excerpt": "年报里没找到相关段落", "st_insufficient": "年报/简介说得不够清楚",
        "st_other": "未核对",
        "excluded_title": "你排除的（{n} 家）",
        "removed_title": "按你的范围回答和你的 AI 的判断移出的（{n} 家）",
        "removed_title_agent": "你的 AI 核对后移出的（{n} 家）",
        "why_scope": "按你的范围回答：{kind}（AI 读摘录后的推断，把握 {p}）",
        "why_scope_agent": "按你的范围回答：{kind}（你的 AI 判为这一类）",
        "why_default": "按你的原话：{kind}（AI 读摘录后的推断，把握 {p}）",
        "why_agent": "你的 AI 判断不要：{why}", "why_agent0": "你的 AI 判断不要",
        "gaps_title": "缺口（没读到的，不是“不相关”）",
        "gap_no_description": "{n} 家没有任何简介，这次没读（不是“不相关”）：{by}。补中国：让你的 AI 运行 {cmd}（约 {m} 分钟，可后台）。",
        "gap_no_description_other": "{n} 家没有任何简介，这次没读（不是“不相关”）：{by}。",
        "gap_us_profile": "美股 {k} 家只用简介核对。想用年报原文核对，可以在下载时按 SEC 规则附上一个名字和邮箱（可选，"
                          "不用注册账号，只发给 sec.gov）：{cmd}。",
        "gap_us_profile_no": "美股 {k} 家只用简介核对（你选择了不提供 SEC 联系方式）。",
        "gap_jp_profile": "日本 {k} 家只用简介核对（这台电脑没有日本年报数据包）。",
        "gap_thin": "简介太薄、要等年报原文才能判断，暂时进不了名单：{names}{why}。",
        "gap_thin_sec": "（美股年报要先设置 SEC 联系方式）", "gap_thin_sec_no": "（美股年报要 SEC 联系方式，你选择了不提供）",
        "gap_ondemand": "这次临时补了 {n} 家的年报原文（{src}）。",
        "footer": "运行 {run} · jev-screen {ver} · 仅供个人研究",
        "dur_ms": "{m} 分 {s} 秒", "dur_s": "{s} 秒",
        "no_js": "这个页面需要浏览器开启 JavaScript 才能完整显示。下面是纯文字名单：",
    },
    "en": {
        "title": "Screen results", "idea": "Your idea", "idea_en": "The English sentence sent to the AI",
        "idea_orig": "As you wrote it", "ai_tr": "AI translation",
        "ai_tr_quote": "AI translation (not the filing text)", "show_orig": "Show original",
        "hide_orig": "Hide original", "orig_label": "Original", "untranslated": "original (not translated)",
        "text_ai": " (AI translation)", "text_untranslated": " (original, not translated)",
        "legend_ai": "AI translation = made by your AI; tap \"Show original\" to check it. The original is the fact",
        "version_first": "Version 1", "version_n": "Version {n}: applied your {k} answers (previous: {prev})",
        "version_n0": "Version {n} (previous: {prev})",
        "meta": "{date} · {status} · cost ${cost}{cny} · took {time}",
        "meta_total": "{date} · {status} · this idea so far: cost ${cost}{cny}, took {time} (not counting time waiting "
                      "for your answers)",
        "version_change": "Version {n}: {change} (previous: {prev})",
        "cny": "", "cny_tiny": "",
        "status_ok": "done", "status_partial": "partly done", "status_budget_exhausted": "budget ran out (partial)",
        "status_other": "not finished",
        "funnel": "Of {universe} companies (market cap ≥ {floor}), {described} have a profile the AI read; {l1} passed "
                  "the first read and {listed} were confirmed from an annual report or profile.",
        "short_note": "Only {n} listed, fewer than 10: that is how many state this business plainly in their own "
                      "texts. The {u} under \"Passed the first read, not confirmed\" below are the ones to confirm.",
        "short_note0": "Only {n} listed, fewer than 10: that is how many state this business plainly in their own "
                       "texts. Try other wording or a wider scope.",
        "short_note_open": "Only {n} listed, fewer than 10, and the list may not be complete: {why}, so it may grow.",
        "short_why_unchecked": "{k} under \"Passed the first read, not confirmed\" below were not checked (the budget "
                               "ran out, the check failed or did not reach them)",
        "short_why_run": "this run did not fully finish",
        "funnel_rescued": "{n} of the listed companies did not pass the first read (a thin or one-sided profile) and "
                          "were listed because their annual report says it.",
        "rank_note": "Rank = how well the company fits your idea (more direct evidence first, then market cap). It "
                     "ignores valuation and financials and is not a recommendation to buy.",
        "banner": "For your personal research only: this page contains personal-use-only TradingView / Yahoo data "
                  "and verbatim annual-report excerpts. Do not forward, post screenshots or upload it.",
        "legend_title": "How to read this page",
        "legend_fact": "Fact = verbatim filing text, with link", "legend_inference": "Inference = the AI's judgment",
        "legend_gap": "Gap = what we could not get or read", "legend_user": "Your call = a judgement you or your AI gave on a calibration question",
        "tag_fact": "Fact", "tag_inference": "Inference", "tag_gap": "Gap", "tag_user": "Your call",
        "search": "Find by name or ticker…",
        "search_none": "Not on this page? Ask your AI to run jevscreen why <name>; it will tell you why",
        "filter_all": "All", "filter_report": "With annual report", "filter_edge": "Borderline",
        "list_title": "The list in original rank order ({n} companies)", "list_empty": "No company passed the check this time.",
        # the confirmed list and the to-confirm section (never padded to 10: owner decision 2026-09-29)
        "funnel_split": "Of {universe} companies (market cap ≥ {floor}), {described} have a profile the AI read; {l1} "
                        "passed the first read and were checked against an annual report or profile.",
        "split_line": "{m} confirmed; {u} more to confirm.", "split_line0": "{m} confirmed.",
        "split_open": "The list may still change: {why}.",
        "chat_top_more": "Above are the first {n} of the confirmed list; {k} more confirmed companies are not listed "
                         "here. The page lists all {m}.",
        "main_title": "Confirmed ({n})", "main_rows_title": "Confirmed: the evidence for each ({n}; tap to open)",
        "main_note": "Inferred: only companies judged central on the first read whose text the AI found states this "
                     "business plainly (\"Stated\" is the AI's reading); evidence under each; not padded to 10.",
        "main_note_judge": "Inferred: only companies whose texts, read item by item, show their own product, a "
                           "supplier role and the idea's target (\"Stated\" is the AI's reading); the list is not "
                           "padded to 10.",
        "main_note_agent": "Plus {n} first left to confirm, then confirmed by your AI from the text (marked "
                           "\"checked by your AI\").",
        "main_note_agent_all": "Inferred: all {n} confirmed were first left to confirm, then your AI found their text "
                               "states this business (marked \"checked by your AI\"); evidence under each; not padded "
                               "to 10.",
        "main_empty": "No company could be confirmed from its own texts this time. The {u} under \"To confirm\" "
                      "below are the place to start.",
        "main_empty0": "No company could be confirmed from its own texts this time. Try other wording or a wider "
                       "scope.",
        "main_empty_open": "No company could be confirmed from its own texts yet.",
        "confirm_title": "To confirm ({n}; not in the confirmed list)",
        "confirm_note": "The AI found them related, but the first read did not judge it their central business, "
                        "the text does not state it plainly, the excerpt shown does not mention the idea, there is "
                        "only a profile, or they were moved down. Read "
                        "the text and decide, or ask your AI to check them.",
        "funnel_rescued_split": "{n} of the companies shown did not pass the first read (a thin or one-sided "
                                "profile) and are shown because their annual report mentions it.",
        "unverified_title_split": "Passed the first read, not listed ({n})",
        "short_why_unchecked_split": "{k} under \"Passed the first read, not listed\" below were not checked (the "
                                     "budget ran out, the check failed or did not reach them)",
        "verdict_main": "Stated", "main_hint": "Inferred: \"Stated\" is the AI's reading.",
        "via_agent": "checked by your AI",
        "via_agent_no": "your AI says it does not fit: moved to confirm",
        "badge_constraint_missing": "The text does not state the idea's market, place or role (moved to Related)",
        "badge_constraint_unclear": "Unclear whether it meets the idea's market, place or role (moved to Related)",
        "badge_constraint_unchecked": "The idea's market, place or role was not checked (budget or service); confirm it yourself",
        "badge_judge_c_product": "Inferred: the AI reading the text sees a component, a part sold only inside its own assembly or an adjacent product, not the idea's product (moved down; your AI reviews it)",
        "badge_judge_c_role": "Inferred: the AI reading the text sees a buyer, an upstream supplier, a distributor, a minority holder or another link, not a supplier (moved down; your AI reviews it)",
        "badge_judge_c_target": "Inferred: the AI reading the text sees only other markets, uses or places (moved down; your AI reviews it)",
        "badge_judge_unchecked": "Not checked item by item (budget or service); confirm it yourself",
        "badge_judge_not_read": "Ranked after the first 150 checked item by item, so not checked; confirm it yourself",
        "badge_second_search": "Inference: the first excerpts said too little; a second search found another passage of the same filing that mentions it, so at most Related. The passage is the fact: read it",
        "verdict_explicit": "Clearly fits", "verdict_partial": "Related", "verdict_edge": "Borderline",
        "evidence_annual_report": "annual report", "evidence_profile": "profile only",
        "evidence_user": "your call (the AI did not confirm it from the text)",
        "no_quote": "(no quotable text)", "quote_from": "Source", "open_link": "Open the filing",
        "details": "Details", "reads_label": "Reads", "reads": "Read {n} times, {k} judged a fit",
        "reads_one": "Read once",
        "l1": "First read (profile)", "l2": "Check", "p_pos": "How sure the AI is that it fits",
        "p_core": "First-read core score (debug)",
        "lbl_core": "Central", "lbl_adjacent": "Adjacent", "lbl_unrelated": "Unrelated", "lbl_insufficient": "Unclear",
        "lbl_explicit": "Clear", "lbl_partial": "Related", "lbl_contradicted": "Contradicted",
        "quote_gap": "The filing excerpt does not mention it (gap)",
        "quote_gap_profile": "The profile excerpt does not mention it (gap)",
        "quote_gap_why": "None of the words of your idea ({terms}) appear in this excerpt. The verdict is only the "
                         "AI's inference; check it yourself.",
        "quote_gap_why_user": "None of the words of your idea ({terms}) appear in this excerpt. This company is "
                              "listed because of your answer.",
        "more": "Show all", "less": "Show less",
        "top_title": "Original top {n}, grouped by verdict (# = original rank)", "col_rank": "#", "col_name": "Company", "col_verdict": "Verdict",
        "col_evidence": "Evidence",
        "ev_short_annual_report": "report", "ev_short_profile": "profile",
        "ev_short_gap": "not in the report", "ev_short_gap_profile": "not in the profile",
        "unverified_group": "{n}: {why}",
        "mcap": "Market cap", "badges": "Marks", "what": "What it does",
        "what_ar": "Main business (annual report)", "what_profile": "The profile says",
        "badge_profile_only": "Profile only (weaker)", "badge_stale": "Filing older than 3 years",
        "badge_edge": "May change on re-reading", "badge_read_once": "Read once",
        "badge_backfill": "Filled in, not checked", "badge_user": "Your call", "badge_unchecked": "not yet checked",
        "badge_l1_rescued": "Found in the annual report (the profile did not say it)",
        "badge_profile_gap_report_target": "Found in the annual report (the profile did not say it)",
        "badge_no_mention": "The excerpt does not mention your idea",
        "badge_scope_no_target": "The excerpt does not name the target (moved down by your scope answer)",
        "badge_scope_unchecked": "Scope answer not checked (budget)",
        "badge_agent_thin": "Too little evidence; your AI could not tell either",
        "cards_title": "Help sharpen the list (optional, about 3–5 min)",
        "card_rank": "Now ranked {rank}", "card_not_listed": "Not listed", "card_why": "Why this card",
        "card_what": "What it does", "card_quote": "Filing text",
        "card_explain": "Hard to read? Tell your AI the card number and ask it to explain",
        "yes": "Yes", "no": "No", "unsure": "Not sure", "clear": "Clear",
        "yes_more": "Optional: which kind?", "no_more": "Optional: why not?",
        "bar_label": "Paste this to your AI:", "copy": "Copy", "copied": "Copied",
        "copy_failed": "Copy failed: select the text above and copy it by hand", "copy_prefix": "Please run: ",
        "no_cards": "No cards need your judgment this time", "bar_empty": "(tap the cards above first)",
        "unverified_title": "Passed the first read, not confirmed ({n})",
        "st_contradicted": "not supported by the filing/profile", "st_skipped_budget": "not checked: budget ran out",
        "st_failed": "check failed", "st_no_excerpt": "no relevant passage in the filing",
        "st_insufficient": "the filing/profile does not say enough", "st_other": "not checked",
        "excluded_title": "You excluded ({n})",
        "removed_title": "Removed by your scope answers and your AI ({n})",
        "removed_title_agent": "Removed after your AI's check ({n})",
        "why_scope": "your scope answer: {kind} (the AI's inference from the excerpt, {p} sure)",
        "why_scope_agent": "your scope answer: {kind} (your AI judged it this kind)",
        "why_default": "your own words: {kind} (the AI's inference from the excerpt, {p} sure)",
        "why_agent": "your AI's call, drop: {why}", "why_agent0": "your AI's call: drop",
        "gaps_title": "Gaps (not read, which is not the same as not relevant)",
        "gap_no_description": "{n} {companies_have} no profile at all and were not read (not \"not relevant\"): {by}. "
                              "To fill China: ask your AI to run {cmd} (about {m} min, can run in the background).",
        "gap_no_description_other": "{n} {companies_have} no profile at all and were not read (not \"not "
                                    "relevant\"): {by}.",
        "gap_us_profile": "{k} US {companies_were} checked from profiles only. To check {them} against annual reports, "
                          "you can give the SEC a contact name and e-mail, sent with each download (optional, no account): {cmd}.",
        "gap_us_profile_no": "{k} US {companies_were} checked from profiles only (you chose not to give the SEC a "
                             "contact).",
        "gap_jp_profile": "{k} Japanese {companies_were} checked from profiles only (no Japanese filing pack on this "
                          "computer).",
        "gap_thin": "Profile too thin, waiting for the annual report before they can be judged (not listed for now): "
                    "{names}{why}.",
        "gap_thin_sec": " (US reports need the SEC contact set first)",
        "gap_thin_sec_no": " (US reports need the SEC contact, which you chose not to give)",
        "gap_ondemand": "Annual-report text was fetched on demand for {n} companies ({src}).",
        "footer": "run {run} · jev-screen {ver} · personal research only",
        "dur_ms": "{m} min {s} s", "dur_s": "{s} s",
        "no_js": "This page needs JavaScript for the full view. The plain-text list follows:",
    },
}
# The one page per idea (owner decision 2026-09-27): prerequisites, progress, scope questions, results.
ONE_PAGE_STRINGS: dict[str, dict[str, str]] = {
    "zh": {"sec_ready": "准备情况", "sec_optional": "可选项（{k}/{n} 已设置；这次结果不需要它们）", "show_checks": "看每一项",
           "sec_progress": "进度", "sec_questions": "帮 AI 把范围定准（可选）",
           "sec_defaults": "已按你的原话自动套用的范围", "undo_default": "撤销这条",
           "undo_intro": "不想这样？复制这行发给你的 AI（免费，几秒更新）：",
           "q_intro": "每题点一个答案，然后把下面这行复制给你的 AI。", "q_line": "复制给你的 AI：",
           "q_empty": "（先在上面点选答案）", "sec_results": "结果",
           "results_wait": "结果会在筛选完成后出现在这里。这页会自己刷新，不用管它。",
           "results_none": "还没有结果。", "rows_title": "每家的证据（按原排名，{n} 家，点开看）",
           "refreshing": "这页每 3 秒自动刷新", "st_ok": "已完成", "st_run": "进行中", "st_wait": "还没开始",
           "st_need": "需要你处理", "st_fail": "出错", "st_opt": "可选", "open_row": "点开看证据",
           "agent_tag": "你的 AI 判断", "agent_yes": "符合", "agent_no": "不符合", "agent_unsure": "拿不准",
           "agent_wait": "等你决定（见上面的问题）", "agent_not_applied": "没采用，按你的回答"},
    "en": {"sec_ready": "Getting ready", "sec_optional": "Optional ({k} of {n} set; this result does not need them)",
           "show_checks": "Show each check", "sec_progress": "Progress",
           "sec_questions": "Help the AI get the scope right (optional)",
           "sec_defaults": "Scope applied automatically from your own words", "undo_default": "Undo this",
           "undo_intro": "Not what you meant? Copy this line to your AI (free, updates in seconds):",
           "q_intro": "Pick one answer per question, then copy the line below to your AI.",
           "q_line": "Paste this to your AI:", "q_empty": "(pick the answers above first)", "sec_results": "Results",
           "results_wait": "The results appear here when the screen is done. This page refreshes by itself; "
                           "nothing to do.",
           "results_none": "No results yet.", "rows_title": "Evidence in original rank order ({n} companies; tap to open)",
           "refreshing": "This page refreshes itself every 3 seconds", "st_ok": "done", "st_run": "running",
           "st_wait": "not started", "st_need": "needs you", "st_fail": "error", "st_opt": "optional",
           "open_row": "Tap to see the evidence",
           "agent_tag": "Your AI's call", "agent_yes": "fits", "agent_no": "does not fit", "agent_unsure": "not sure",
           "agent_wait": "waiting for you (see the questions above)", "agent_not_applied": "not used: your answer "
                                                                                         "wins"},
}
for _lg, _t in ONE_PAGE_STRINGS.items():
    STRINGS[_lg].update(_t)
REFRESH_S = 3            # the page reloads itself this often while work runs (REFRESH_JS; <noscript> meta refresh)
PAUSE_S = 4              # ... but not within this long of someone using it (a pointer pressed, a key, the sand played)

# The reload while work runs (read from <meta name="jevscreen-refresh">): every REFRESH_S seconds, but never within
# PAUSE_S of a pointer pressed or a key anywhere, or of the sand drawing being hovered or dragged (it stamps
# window.__jevSandBusy), so a tooltip being read or a drag is never cut off; the page's state is saved first.
REFRESH_JS = r"""
(function(){try{var m=document.querySelector('meta[name="jevscreen-refresh"]');var s=m?Number(m.getAttribute('content')):0;if(!(s>0))return;
var W=window,t0=Date.now(),busy=0,gone=false;function poke(){busy=Date.now();}
document.addEventListener('pointerdown',poke,true);document.addEventListener('keydown',poke,true);
setInterval(function(){if(gone)return;var n=Date.now(),b=Math.max(busy,Number(W.__jevSandBusy)||0);
 if(n-t0>=s*1000&&n-b>=%(pause)d){gone=true;try{if(W.__jevSave)W.__jevSave();}catch(e){}W.location.reload();}},250);
}catch(e){}})();
""" % {"pause": PAUSE_S * 1000}

STATUS_KEYS = {"contradicted": "st_contradicted", "skipped_budget": "st_skipped_budget", "failed": "st_failed",
               "uncertain": "st_failed", "no_excerpt": "st_no_excerpt", "insufficient": "st_insufficient"}


# --------------------------------------------------------------------------------------------------- helpers

def _json_for_script(obj: Any) -> str:
    """JSON safe inside <script type="application/json">: no '</script>', no HTML comment, no JS line breaks."""
    text = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return (text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def _safe_url(u: Any) -> str | None:
    """The URL when it is a plain http(s) URL without control characters or spaces, else None."""
    if not isinstance(u, str):
        return None
    u = u.strip()
    if not re.match(r"^https?://[^\s\"'<>\\]+$", u, re.IGNORECASE) or any(ord(c) < 32 for c in u):
        return None
    return u


def _ticker(security_id: str | None) -> str:
    return str(security_id or "").split(":", 1)[-1]


def _first_sentence(text: str | None, n: int = ONE_LINE_CHARS) -> str | None:
    from . import calib
    try:
        return calib._first_sentence(text, n)
    except Exception:  # noqa: BLE001
        t = (text or "").strip()
        return t[:n] or None


_AR_HEAD = [re.compile(p) for p in (
    r"^\[[^\]]*\]\s*",                                          # an '[annual report excerpts: ...]' tag
    r"^第[一二三四五六七八九十]{1,3}[节章]\s*",                          # 第三节
    r"^[（(][一二三四五六七八九十\d０-９]{1,3}[)）]\s*",                  # （一）
    r"^[一二三四五六七八九十]{1,3}[、.．]\s*",                            # 一、
    r"^[\d０-９]{1,2}(?:[、.．)）]\s*|\s+|(?=【))",                      # 1、 / 3 / ３ / ３【
    r"^【[^】]{1,20}】\s*",                                            # 【事業の内容】
    r"^Item\s+\d+[A-Z]?\.?\s*",                                       # Item 1.
    r"^(?:Business(?:\s+Overview)?|Overview|General|Our\s+Company)\s*[:.\-–]?\s+(?=[A-Z])",
    r"^(?:报告期内)?(?:公司)?(?:所)?(?:从事的)?主(?:要|营)业务(?:或产品)?(?:简介|情况|概要|概述|概况)?[：:]?\s+",
    # a long CNINFO heading without a full stop ('公司所从事的主要业务、主要产品及其用途、…行业地位情况'), with or
    # without a space before the text
    r"^(?:报告期内)?(?:公司)?所?从事的?主(?:要|营)业务[、，,][^。\s]{0,100}?(?:情况|概况|简介|说明)[：:]?"
    r"(?:\s+|(?=(?:本?公司|本?集团)))",
)]
_AR_NOT_BUSINESS = re.compile(r"营业收入|净利润|归属于|同比|net income|revenues? (?:of|were|was) |annual report|"
                              r"about the cover|dear (?:share|stake)holders|forward-looking|"
                              r"以下|次のとおり|如下|・・・|as follows|"    # a pointer to a table, not the business
                              r"(?:[\u3040-\u9fff] ){3}", re.I)              # a table header row ('事 業 区 分')
# a business sentence says what the company makes, sells or does
_AR_BUSINESS = re.compile(r"是一家|从事|主营|生产|销售|提供|研发|制造|服务|业务|產品|製造|販売|営んで|事業|사업|제조|판매|"
                          r"\b(?:is|are)\s+(?:a|an|the|one)\b|\b(?:design|manufactur|produc|provid|develop|operat|"
                          r"offer|sell|engag|make|suppl|distribut|own)\w*\b", re.I)
_ASIDE = re.compile(r"[（(][^（）()]{0,80}[。．.][^（）()]{0,80}[)）]")
# a sentence that only says how many subsidiaries make up the group (EDINET '...で構成されております。'): the next one
# says the business
_AR_STRUCTURE = re.compile(r"構成されて(?:おり|い)ます。$|(?:以下|次)のとおり(?:であります|です)。$|"
                           r"^The (?:Company|Group) (?:consists|is composed) of [^.]*subsidiaries\.$")


# the sentence is about the company: it names it (a pronoun or its own name) near its start
_AR_SUBJECT = re.compile(r"公司|集团|集團|本行|当社|同社|弊社|企業集団|企業グループ|グループは|株式会社|당사|동사|회사|연결기업|"
                         r"연결실체|지배기업|\(주\)|㈜|"
                         r"(?i:\bwe\b|\bthe (?:company|group|bank|firm|partnership|trust)\b|\bour (?:company|group)\b|"
                         r"\b(?:inc|corp|corporation|incorporated|co|ltd|limited|plc|n\.v|s\.a|ag|se|group|holdings?)\b\.?)")
# or a name (unknown to the caller) that describes itself: '澜起科技是一家…', 'Danaher is a global…'
_AR_SELF = re.compile(r"^(?P<who>[^，,。、；;:：]{2,20}?)(?:是一家|系一家|为一家|是一个|是全球|是国内|是中国|是我国|是业内|"
                      r"为国内|为全球|作为国内|作为全球)|"
                      r"^(?P<who_v>[^，,。、；;:：\s]{2,10}?)(?:主要从事|主营业务|专注于|致力于|深耕|成立于)|"
                      r"^(?P<who_en>[^.;:,]{2,50}?) (?:is|was) (?:a|an|one of|the)\b")
_NOT_WHO = re.compile(r"行业|产业|產業|市场|市場|我国|industry|market|sector", re.I)
_SUBJECT_WITHIN = {True: 20, False: 40}         # CJK characters / Latin characters from the start
# about the economy, the industry, a definition or the legal set-up, not the business (checked on the whole sentence,
# before it is clipped)
_AR_NOT_SUBJECT = re.compile(r"^(?:20\d\d年|我国|当前|近年来|全球)|是指|^[^，,。]{0,20}作为[^，,。]{0,30}(?:核心|关键)|"
                             r"is an exempted company|incorporated under|(?:is|was|were) (?:an? )?(?:incorporated|organized) in\b|"
                             r"(?-i:is an? [A-Z][a-z]+ corporation\b)|legal and commercial name|"
                             r"statements in this (?:document|report)|^the terms\b|^unless the context|fiscal year|재무구조|"
                             r"\|", re.I)


def _name_tokens(names: Any) -> list[str]:
    """What names the company near a sentence's start: each name as given (a Chinese short name), and its first
    Latin word ('Teradyne' of 'Teradyne, Inc.')."""
    out: list[str] = []
    for nm in names or []:
        nm = str(nm or "").strip()
        if not nm:
            continue
        out.append(nm)
        first = re.split(r"[\s,.(（]", nm, maxsplit=1)[0]
        if len(first) >= 2 and first != nm:
            out.append(first)
    return out


def _about_the_company(one: str, names: list[str], cjk: bool) -> bool:
    head = one[:_SUBJECT_WITHIN[cjk]]
    if _AR_NOT_SUBJECT.search(one):
        return False
    if _AR_SUBJECT.search(head) or any(nm.lower() in head.lower() for nm in names):
        return True
    m = _AR_SELF.search(one)
    return bool(m) and not _NOT_WHO.search(m.group("who") or m.group("who_v") or m.group("who_en") or "")


def business_sentence(text: str | None, n: int = ONE_LINE_CHARS, names: Any = None) -> str | None:
    """The first sentence of an annual report's business overview (the excerpt L2 read as kind 'overview'), without
    its section heading or numbering: the official line of what the company does. The sentence must be about the
    company (公司 / 当社 / 당사 / We / The Company or one of `names` near its start) and not about the economy, the
    industry, a definition, the legal set-up or a table; a rejected sentence gives way to the next one once. None
    when nothing is left: the caller keeps the profile line."""
    if not text or not str(text).strip():
        return None
    t = re.sub(r"\s+", " ", str(text)).strip()
    for _ in range(4):
        prev = t
        for rx in _AR_HEAD:
            t = rx.sub("", t, count=1).strip()
        if t == prev:
            break
    t = t.replace("｡", "。").replace("､", "、")        # half-width CJK punctuation of some PDF extractions
    t = _ASIDE.sub("", t)                   # an aside with its own full stop would end the sentence early
    toks = _name_tokens(names)
    for _attempt in range(2):               # the first sentence, then the next one once
        full = _first_sentence(t, 100_000)  # the whole sentence: the checks run before it is clipped
        if not full:
            return None
        at = t.find(full)
        rest = t[at + len(full):].strip() if at >= 0 else ""
        cjk = sum(1 for ch in full if "\u3040" <= ch <= "\u9fff" or "\uac00" <= ch <= "\ud7af") > 0
        ok = not ((cjk and len(full) < 12) or (not cjk and len(full) < 25) or _AR_NOT_BUSINESS.search(full)
                  or not _AR_BUSINESS.search(full) or _AR_STRUCTURE.search(full)
                  or not _about_the_company(full, toks, cjk))
        if ok:
            return _first_sentence(full, n)
        if not rest:
            return None
        t = rest
    return None


def _clip(text: str | None, n: int) -> str | None:
    if not text:
        return None
    t = re.sub(r"\s+", " ", str(text)).strip()
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"


def _money(v: float | None) -> str:
    return "?" if v is None else f"{v:.2f}" if v >= 0.1 else f"{v:.4f}"


def _floor_text(v: float | None, lang: str = "en") -> str:
    if lang == "zh":
        return l10n.usd_words(v or 0, "zh")
    if not v:
        return "$0"
    return f"${v / 1e9:g}B" if v >= 1e9 else f"${v / 1e6:g}M"


def _duration(s: float | None) -> str:
    if s is None:
        return "?"
    s = int(round(s))
    return f"{s // 60} min {s % 60} s" if s >= 60 else f"{s} s"


def _badges(r: dict[str, Any], today: dt.date | None, no_mention: bool = False) -> list[str]:
    out = ["no_mention"] if no_mention else []
    if r.get("l2_evidence") == "profile":
        out.append("profile_only")
    if r.get("l2_doc_stale"):
        out.append("stale")
    if r.get("l2_edge"):
        out.append("edge")
    note = r.get("l2_read_note") or ""
    if isinstance(note, str) and note.startswith("只读了"):
        out.append("read_once")
    if r.get("backfill"):
        out.append("backfill")
    if r.get("scope_demoted"):
        out.append("scope_no_target")          # a scope answer moved it to the end (scope design §5.3)
    if r.get("scope_unchecked"):
        out.append("scope_unchecked")
    if r.get("agent_thin"):
        out.append("agent_thin")
    if r.get("l1_rescued"):
        out.append("l1_rescued")               # the profile missed it; its annual report says it (screen.l1_rescued)
    if r.get("l2_constraint") in ("missing", "unclear", "unchecked"):
        # screen --l2-constraints moved it to partial (missing / unclear), or could not check it (unchecked)
        out.append(f"constraint_{r['l2_constraint']}")
    if r.get("judge_tier") == "C":
        # screen --judge: an inference from the text (jevscreen.atomic), one badge per kind of counter-evidence;
        # none once a human pin or your AI's applied yes moved the row up (the row is no longer 'moved down')
        from . import atomic
        if not atomic.is_lifted(r):
            out += [f"judge_c_{x.split(':')[0]}" for x in r.get("judge_reason") or []]
    elif r.get("judge_state") in ("unchecked", "not_read"):
        out.append(f"judge_{r['judge_state']}")
    if r.get("l2_second_search") == "raised":
        out.append("second_search")            # screen --second-search raised it from insufficient (at most partial)
    return [b for b in BADGE_ORDER if b in out]


# Words of an idea that say nothing about what it is about ('core suppliers of ... systems'): they do not count
# when the page checks whether an excerpt mentions the idea (idea_terms / mentions_idea).
GENERIC_EN = frozenset("""main core key leading leader leaders major top big large primary chief pure play
supplier suppliers supply provider providers vendor vendors maker makers manufacturer manufacturers producer producers
company companies firm firms business businesses system systems solution solutions management product products
service services technology technologies tech industry industries global market markets player players related
the of for and with in to on by from a an as or at its their that which who""".split())
GENERIC_CJK = frozenset({"系统", "核心", "供应", "应商", "厂商", "公司", "产品", "业务", "技术", "服务", "相关", "主要",
                         "解决", "方案", "企业", "行业", "设备", "市场", "领域", "发展", "生产", "销售", "制造", "提供",
                         "龙头", "领先", "管理", "研发", "中国", "国内", "全球", "以及", "概念", "标的", "个股",
                         "シス", "ステ", "テム", "会社", "製品", "事業", "サー", "ービ", "ビス", "시스", "스템", "회사", "제품",
                         "사업", "기업"})
_CJK_FUNCTION = frozenset("的和与及或在为是等之了对于把被将向从其各该此并也都")
_CJK_RUN = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]+")
_SHARE_CLASS = re.compile(r"\s*[-,]?\s+\(?(?:class|cl\.?|series)\s+[a-z0-9]\.?\)?(?:\s+(?:shares|common stock|ordinary "
                          r"shares))?$|\s*[-,]?\s+\(?(?:ordinary shares|common stock|common shares|sponsored adr|adr|ads|"
                          r"american depositary (?:shares|receipts))\)?$", re.I)


def _fold(t: str) -> str:
    return unicodedata.normalize("NFKC", t).strip().casefold()


def idea_terms(result: dict[str, Any]) -> list[str]:
    """The words the page looks for when it checks that an excerpt talks about the idea: the run's excerpt terms
    (every language) and the sieve's learned keywords, minus the weak ones and generic words; the idea and its
    English sentence split into words. A CJK phrase longer than 4 characters is also split into its 2-character
    pieces (储能电站液冷温控系统 -> 储能, 电站, 液冷, 温控, ...; a piece overlapping a generic word such as 系统 or
    核心 is left out), because a filing rarely repeats the whole phrase."""
    raw: list[str] = []
    tb = result.get("terms_by_lang")
    if isinstance(tb, dict):
        for v in tb.values():
            raw += [str(t) for t in v or []]
    elif isinstance(tb, (list, tuple)):
        raw += [str(t) for t in tb]
    else:
        raw += [str(t) for t in result.get("terms") or []]
    cal = result.get("calibration") if isinstance(result.get("calibration"), dict) else {}
    for v in (cal.get("keywords_add") or {}).values():
        raw += [str(t) for t in v or []]
    for idea in (result.get("idea"), result.get("idea_en")):
        if idea:
            raw += re.findall(r"[A-Za-z][A-Za-z0-9\-]+", str(idea)) + _CJK_RUN.findall(str(idea))
    weak = {_fold(t) for v in (cal.get("keywords_weak") or {}).values() for t in v or []}
    out: list[str] = []

    def add(t: str) -> None:
        if t and _fold(t) not in weak and t not in out:
            out.append(t)
    for t in raw:
        t = unicodedata.normalize("NFKC", t).strip()
        if not t or _fold(t) in weak:
            continue
        runs = _CJK_RUN.findall(t)
        if runs:
            for run in runs:
                if len(run) <= 4 and run not in GENERIC_CJK and len(run) >= 2 and not (set(run) & _CJK_FUNCTION):
                    add(run)
                if len(run) > 2:
                    for i in range(len(run) - 1):
                        bg = run[i:i + 2]
                        # a piece that overlaps a generic word ('心供' of 核心供应商, '控系' of 温控系统) is noise
                        if bg in GENERIC_CJK or set(bg) & _CJK_FUNCTION or \
                                (i > 0 and run[i - 1:i + 1] in GENERIC_CJK) or run[i + 1:i + 3] in GENERIC_CJK:
                            continue
                        add(bg)
            continue
        words = [w for w in re.split(r"\s+", t) if w]
        content = [w for w in words if w.lower().strip(".,;:") not in GENERIC_EN and (len(w) >= 3 or w.isupper())]
        if content:
            add(t if len(words) > 1 else content[0])
    return out


def idea_words_shown(result: dict[str, Any], lang: str, n: int = 8) -> list[str]:
    """A few of the idea's words to name on the page next to a 'does not mention it' excerpt: the idea's CJK phrases
    cut into 2-character words from their start (储能电站液冷温控 -> 储能、电站、液冷、温控) and its English content
    words, the reader's language first. Only words the check itself looks for (idea_terms, so never a weak keyword):
    a word named as missing is never one the excerpt contains. Without such a word: the first idea_terms."""
    cjk: list[str] = []
    en: list[str] = []
    for text in (result.get("idea"), result.get("idea_en")):
        t = unicodedata.normalize("NFKC", str(text or ""))
        pieces = [x for run in _CJK_RUN.findall(t) for x in re.split("[" + "".join(sorted(_CJK_FUNCTION)) + "]", run)]
        for run in pieces:
            for i in range(0, len(run) - 1, 2):
                w = run[i:i + 2]
                if w not in GENERIC_CJK and not (set(w) & _CJK_FUNCTION) and w not in cjk:
                    cjk.append(w)
        for w in re.findall(r"[A-Za-z][A-Za-z0-9\-]+", t):
            if w.lower() not in GENERIC_EN and (len(w) >= 3 or w.isupper()) and w not in en:
                en.append(w)
    terms = idea_terms(result)
    checked = {_fold(t) for t in terms}
    # the reader's language only (a zh page names Chinese words, an en page English ones), when there are any
    own = [w for w in (cjk if lang == "zh" else en) if _fold(w) in checked]
    if own:
        return own[:n]
    same = [t for t in terms if _script(t) == ("cjk" if lang == "zh" else "latin")]
    words = [w for w in (cjk + en if lang == "zh" else en + cjk) if _fold(w) in checked]
    return (same or words or terms)[:n]


def _script(text: str) -> str | None:
    """'cjk' or 'latin': the script most of the text's letters are in (None: no letters)."""
    cjk = sum(len(x) for x in _CJK_RUN.findall(text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if not cjk and not latin:
        return None
    return "cjk" if cjk * 2 >= latin else "latin"


def mentions_idea(text: str | None, terms: list[str]) -> list[str] | None:
    """The idea terms the text contains (screen._term_pattern: whole words, simple plurals, Simplified /
    Traditional forms); None when there is nothing to check: no text, no terms, or no term in the text's own script
    (Chinese words cannot be looked for in an English excerpt, nor English words in a Chinese one: that is not a
    gap, just a check we cannot make)."""
    if not text or not terms:
        return None
    script = _script(text)
    if script is None or not any(_script(t) == script for t in terms):
        return None
    from . import screen
    return [t for t in terms if screen._term_pattern(t).search(text)]


def _hit_at(text: str, hits: list[str]) -> int:
    """Where the first of `hits` starts in `text` (0 when none is found)."""
    from . import screen
    found = [m.start() for m in (screen._term_pattern(t).search(text) for t in hits) if m]
    return min(found) if found else 0


def _window(text: str, pos: int, n: int = QUOTE_CHARS) -> str:
    """At most n characters of `text` around `pos`: from the start of pos's sentence when that is close, else a
    little before it; '…' marks a cut."""
    t = re.sub(r"\s+", " ", text).strip()
    if len(t) <= n:
        return t
    cut = max(t.rfind(ch, 0, pos) for ch in "。！？；.!?;")
    start = cut + 1 if cut >= 0 and pos - cut <= 150 else max(0, pos - 60)
    body = t[start:].strip()
    head = "…" if start > 0 else ""
    room = n - len(head)
    return head + (body if len(body) <= room else body[:room - 1].rstrip() + "…")


def load_l2_pieces(run_dir: str | Path | None, keys: Iterable[str] | None = None) -> dict[str, dict[str, Any]]:
    """company_key -> {'sha', 'text', 'pieces', 'overview'} from <run_dir>/l2_inputs.jsonl. `text` is the exact L2
    input, needed to resolve the agent's sentence numbers; `pieces` supports the page's quote selection; `overview`
    the annual report's business overview excerpt (the one-line 'what it does' prefers its first sentence)."""
    if not run_dir:
        return {}
    want = set(keys) if keys is not None else None
    lines: list[dict[str, Any]] = []
    try:
        with open(Path(run_dir) / "l2_inputs.jsonl", encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                ck = d.get("company_key") if isinstance(d, dict) else None
                if ck and (want is None or ck in want):
                    lines.append(d)
    except OSError:
        return {}
    return l2_pieces_of_lines(lines)


def _clean_name(name: str | None) -> str | None:
    """The name without a share-class tail ('... Co., Ltd. Class A' -> '... Co., Ltd.')."""
    if not name:
        return name
    n, prev = str(name).strip(), None
    while n != prev:                      # 'Foo Inc. Class A ADS' -> 'Foo Inc.'
        prev, n = n, _SHARE_CLASS.sub("", n).strip()
    return n or str(name)


def _same_as_name(one_line: str | None, name: str | None) -> bool:
    """True when the 'what it does' line only repeats the company name ('Guangzhou Goaland Energy Conservation
    Tech.' for 'Guangzhou Goaland Energy Conservation Tech Co. Ltd. Class A')."""
    if not one_line or not name:
        return False
    from . import names
    a, b = names.normalize(one_line), names.normalize(name)
    if not a:
        return True
    return a == b or a in b or (b in a and len(a) <= len(b) + 12)


def _reads(r: dict[str, Any]) -> dict[str, int] | None:
    detail = [d for d in r.get("l2_read_detail") or [] if isinstance(d, dict) and d.get("p_pos") is not None]
    if not detail:
        return None
    return {"n": len(detail), "k": sum(1 for d in detail if (d.get("p_pos") or 0) >= 0.5)}


FORM_WORDS = l10n.FORM_WORDS          # filing form ids in plain words (never an internal id on a page)
form_words = l10n.form_words


def _quote(r: dict[str, Any], lang: str = "zh") -> dict[str, Any] | None:
    text = _clip(r.get("evidence_excerpt"), QUOTE_CHARS)
    if not text:
        return None
    profile = r.get("l2_evidence") == "profile"
    src = "profile" if profile else (l10n.source_key(r.get("filing_source")) or r.get("filing_source"))
    date = str(r.get("filing_date") or "")[:10] or None
    return {"text": text, "source": src, "profile": profile,
            "where": l10n.source_form_words(src, None if profile else r.get("filing_form"), lang),
            "date": date, "date_text": None if profile else l10n.date_words(date, lang),
            "url": _safe_url(r.get("evidence_url"))}


def _l2_hit(r: dict[str, Any], terms: list[str], l2: dict[str, Any] | None) -> tuple[str, list[str]] | None:
    """(quote text, hits) from the first piece of the text layer 2 read for this row (l2_inputs.jsonl, the same
    document: evidence_sha agrees when both are known) that contains one of the idea's words; None without one."""
    if not l2 or not terms:
        return None
    if r.get("evidence_sha") and l2.get("sha") and r["evidence_sha"] != l2["sha"]:
        return None
    for piece in l2.get("pieces") or []:
        hits = mentions_idea(piece, terms)
        if hits:
            return _window(piece, _hit_at(re.sub(r"\s+", " ", piece).strip(), hits)), hits
    return None


def _verdict_terms(result: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Target application and product words from scope, or conservatively from the original idea."""
    scope = result.get("scope") or {}
    facets = scope.get("facets") or {}
    facets_zh = scope.get("facets_zh") or {}
    target_phrases = [str(t).strip() for t in (facets.get("target"), facets_zh.get("target")) if t]
    categories = [str(t).strip() for t in (facets.get("category"), facets_zh.get("category")) if t]
    if not target_phrases:
        # Facets are optional in quickstart. Many ideas spell out their application after "for" in the fixed
        # English sentence; a Chinese "...的核心供应商" usually puts that application before the product.
        en = str(result.get("idea_en") or "")
        parts = re.split(r"\bfor\b", en, maxsplit=1, flags=re.I)
        if len(parts) == 2:
            categories.append(parts[0])
            target_phrases.append(parts[1])
            zh = str(result.get("idea") or "")
            pre, sep, suffix = zh.partition("的")
            if sep and any(w in suffix for w in ("供应商", "制造商", "生产商")):
                words = idea_words_shown({"idea": pre}, "zh", n=24)
                if len(words) >= 4:
                    mid = len(words) // 2
                    target_phrases.extend(words[:mid])
                    categories.extend(words[mid:])
    targets = list(dict.fromkeys(t for phrase in target_phrases for t in
                                 ([phrase] if _script(phrase) == "latin" else idea_terms({"idea": phrase}))))
    product = list(dict.fromkeys(t for phrase in categories for t in idea_terms({"idea": phrase}))) \
        if categories else idea_terms(result)
    # A word repeated in the target (e.g. "agent" in "AI agents") is not enough to prove the product.
    product = [t for t in product if not any(mentions_idea(target, [t]) for target in target_phrases)]
    return targets, product


def _product_match(text: str, products: list[str]) -> bool:
    hits = mentions_idea(text, products) or []
    if not hits:
        return False
    same_script = [p for p in products if _script(p) == _script(text)]
    return len(set(hits)) >= min(2, len(same_script)) if _script(text) == "latin" else True


_APPLICATION_BACKREF = re.compile(
    r"(?:该|上述|这些|这类|此类|本)(?:产品|设备|机组|系统|装置)|"
    r"(?:^|[。；])(?:产品|设备|机组|系统|装置)(?:用于|适用于|服务于|面向)|"
    r"\b(?:these|those|such|the|our|this)\s+(?:units?|products?|systems?|devices?|equipment)\b|"
    r"\b(?:it\s+is|they\s+are)\s+(?:used|deployed)\b", re.I)


def _direct_application(parts: list[str], targets: list[str], products: list[str]) -> bool:
    """Cited sentences link a product to the target application, not merely mention them separately."""
    joined = " ".join(parts)
    if not _product_match(joined, products):
        return False
    if not targets:
        hits = mentions_idea(joined, products) or []
        return len(set(hits)) >= min(2, len(products))
    for j, sentence in enumerate(parts):
        if not mentions_idea(sentence, targets):
            continue
        if _product_match(sentence, products):
            return True
        if j and _APPLICATION_BACKREF.search(sentence) and _product_match(parts[j - 1], products):
            return True
    return False


def _cited_direct_match(r: dict[str, Any], l2: dict[str, Any] | None,
                        targets: list[str], products: list[str]) -> str | None:
    """A cited sentence on the row's current evidence that supports a clear page verdict, if any."""
    if r.get("agent_verdict") != "yes" or r.get("agent_state") != "applied" \
            or r.get("agent_level") != "explicit" or not l2 or not l2.get("text") or not products:
        return None
    if not r.get("evidence_sha") or l2.get("sha") != r["evidence_sha"]:
        return None
    ids = r.get("agent_quote_ids")
    if not isinstance(ids, list) or not 1 <= len(ids) <= 3 \
            or any(not isinstance(i, int) or isinstance(i, bool) for i in ids):
        return None
    from . import review
    sentences = dict(review.sentences(l2["text"]))
    # a cited sentence of the stored filing that the excerpt left out (the deck's evidence.more) is kept with the
    # verdict as your AI was shown it
    quotes = r.get("agent_quotes") if isinstance(r.get("agent_quotes"), dict) else {}
    if any(i not in sentences and str(i) not in quotes for i in ids):
        return None
    parts = [quotes.get(str(i)) or sentences[i] for i in ids]
    cited = " ".join(parts)
    if len(cited) > 900:  # a strong verdict must show all of the cited evidence, without hiding the target
        return None
    return cited if _direct_application(parts, targets, products) else None


def _report_fills_profile_target(r: dict[str, Any], profile: str | None, l2: dict[str, Any] | None,
                                 targets: list[str], products: list[str]) -> str | None:
    """The annual text names the target and product in one excerpt; the full L1 profile names no target.

    This is a source gap, separate from `l1_rescued` (which means L1 actually rejected the company). A row may
    pass L1 on its broad category yet need its annual report for the idea's specific application.
    """
    if r.get("l1_rescued") or r.get("l2_evidence") != "annual_report" or not profile or not l2 \
            or not targets or not products or l2.get("sha") != r.get("evidence_sha"):
        return None
    from . import screen
    has = lambda s, ts: any(screen._term_pattern(t).search(s) for t in ts)  # noqa: E731
    if has(profile, targets):
        return None
    from . import review
    return next((piece for piece in l2.get("pieces") or []
                 if _direct_application([s for _, s in review.sentences(piece)], targets, products)), None)


def _row(r: dict[str, Any], one_line: str | None, today: dt.date | None, terms: list[str] | None = None,
         local_name: str | None = None, l2: dict[str, Any] | None = None, lang: str = "zh") -> dict[str, Any]:
    uv = r.get("user_verdict")
    user = bool(uv) and r.get("verdict_source") in ("user", "evidence+user")
    q = _quote(r, lang)
    hits = mentions_idea((q or {}).get("text"), terms or [])
    if q is not None and hits == []:
        # the shown excerpt is only the start of what the AI read: a later piece that names the idea is the quote
        better = _l2_hit(r, terms or [], l2)
        if better is not None:
            q["text"], hits = better
    no_mention = hits is not None and not hits and not user
    if q is not None:
        q["mentions"] = None if hits is None else bool(hits)
    name = _clean_name(r.get("name")) or r.get("security_id")
    if _same_as_name(one_line, name):
        one_line = _first_sentence(r.get("evidence_excerpt"))
        if _same_as_name(one_line, name):
            one_line = None
    badges = _badges(r, today, no_mention)
    return {"rank": r.get("rank"), "name": name, "name_zh": local_name, "ticker": _ticker(r.get("security_id")),
            "security_id": r.get("security_id"), "country": r.get("country"),
            "country_zh": COUNTRY_ZH.get(r.get("country") or ""),
            "country_text": l10n.country_words(r.get("country"), lang),
            "mcap_usd": r.get("market_cap_usd"), "one_line": one_line,
            "mcap_text": l10n.usd_words(r.get("market_cap_usd"), lang) if r.get("market_cap_usd") else None,
            "verdict": "partial",  # the page's three-level verdict is set after unchecked rows are marked
            "evidence": r.get("l2_evidence") or "profile", "quote": q, "badges": badges,
            "edge": "edge" in badges or no_mention,
            "user": user,
            # listed only because of the user's answer: the AI did not confirm it from the text
            "user_only": user and r.get("l2_label") not in ("explicit", "partial"),
            "details": {"reads": _reads(r), "l1": r.get("l1_label"), "l2": r.get("l2_label"),
                        "p_pos": r.get("l2_p_pos"), "p_core": None if r.get("l1_p_core") is None
                        else round(float(r["l1_p_core"]), 2)},
            # your AI's call on this row (its own layer, never the human's) and a scope answer's demotion
            "agent": ({"v": r.get("agent_verdict"), "state": r.get("agent_state"), "chip": r.get("agent_chip"),
                       "why": r.get(f"agent_why_{lang}")} if r.get("agent_verdict") else None),
            "scope_demoted": bool(r.get("scope_demoted")),
            # screen --shortlist: high (L1 core + L2 explicit) / confirm; None without the lever
            "tier": r.get("shortlist_tier") if r.get("shortlist_tier") in ("high", "confirm") else None,
            # moved between the two by your AI's review ('agent': into the main list, 'agent_no': out of it) or the
            # human's pin ('user')
            "main_via": r.get("main_via") if r.get("main_via") in ("agent", "agent_no", "user") else None,
            "constraint": r.get("l2_constraint"), "second": r.get("l2_second_search")}


def shown_gap(r: dict[str, Any], terms: list[str], targets: list[str], products: list[str],
              l2: dict[str, Any] | None) -> str | None:
    """Why the page would call this row 边缘 / Borderline, the same decision build_page_data makes: 'no_mention' (the
    excerpt it shows, after looking through the rest of the text layer 2 read, has none of the idea's words) or
    'edge' (the repeated reads were split, l2_edge); None when the page shows a plain verdict: no such gap, a cited
    sentence of your AI's applied explicit yes that links the product to the target (the page then says 明确 /
    Clearly fits), or a row the human answered. A scope demotion is not counted here (it has its own reason).
    shortlist.gaps uses it so that the confirmed list never holds a row its own page marks borderline."""
    if r.get("verdict_source") in ("user", "evidence+user") and r.get("user_verdict"):
        return None
    q = _quote(r)
    hits = mentions_idea((q or {}).get("text"), terms or [])
    if q is not None and hits == []:
        better = _l2_hit(r, terms or [], l2)
        if better is not None:
            hits = better[1]
    no_mention = hits is not None and not hits
    if not (no_mention or r.get("l2_edge")):
        return None
    if (q and not r.get("scope_demoted") and r.get("l2_constraint") not in ("missing", "unclear")
            and r.get("l2_second_search") != "raised" and _cited_direct_match(r, l2, targets, products)):
        return None
    return "no_mention" if no_mention else "edge"


def gap_check(result: dict[str, Any], l2_pieces: dict[str, dict[str, Any]] | None = None):
    """A function row -> shown_gap(row) for the rows of `result` (its idea words, scope target and product words;
    `l2_pieces` company_key -> the text layer 2 read, default the run folder's l2_inputs.jsonl)."""
    terms = idea_terms(result)
    targets, products = _verdict_terms(result)
    if l2_pieces is None:
        l2_pieces = load_l2_pieces(result.get("output_dir"), [r.get("company_key") for r in result.get("rows") or []])

    def check(r: dict[str, Any]) -> str | None:
        return shown_gap(r, terms, targets, products, (l2_pieces or {}).get(r.get("company_key") or ""))
    return check


def l2_pieces_of_lines(lines: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """load_l2_pieces for l2_inputs.jsonl lines already in memory (screen, before the file is written)."""
    out: dict[str, dict[str, Any]] = {}
    for d in lines or []:
        ck = d.get("company_key") if isinstance(d, dict) else None
        if not ck:
            continue
        ex = d.get("excerpts")
        pieces = [str(e.get("text")) for e in ex if isinstance(e, dict) and e.get("text")] \
            if isinstance(ex, list) else []
        overview = next((str(e["text"]) for e in ex if isinstance(e, dict) and e.get("kind") == "overview"
                         and e.get("text")), None) if isinstance(ex, list) else None
        if not pieces and d.get("text"):
            body = str(d["text"])
            if body.startswith("["):
                body = body.split("\n", 1)[1] if "\n" in body else ""
            pieces = [body.strip()] if body.strip() else []
        out[ck] = {"sha": d.get("evidence_sha"), "text": d.get("text"), "pieces": pieces, "overview": overview}
    return out


def _card_why_en(c: dict[str, Any], max_out: int | None, facets: dict[str, Any] | None = None) -> str:
    from . import calib
    q = c.get("quote") or {}
    slot = c.get("type") if c.get("type") in calib.WHY_EN else "boundary_in"
    v = c.get("verdict") or {}
    target = str((facets or {}).get("target") or "").strip() or "what the idea is about"
    try:
        return calib.WHY_EN[slot].format(rank=c.get("rank"), target=target, pi=c.get("pi") or 0.0,
                                         p=v.get("p_pos") or 0.0, max_out=max_out or 40,
                                         src="profile" if q.get("source") == "公司简介" else "annual report")
    except (KeyError, ValueError, IndexError):
        return ""


def _cards(deck: dict[str, Any] | None, max_out: int | None, lang: str = "zh"
           ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from . import calib
    if not deck or not deck.get("cards"):
        return [], {}
    tokens = calib.answer_tokens(deck)
    chips = deck.get("chips") or {}
    zh_yes = chips.get("yes") or {}
    zh_no = chips.get("no") or {}
    # the English twins carry the sieve's English facets (deck['facets_en']); without them (an older deck) the
    # parenthetical is left out rather than filled with placeholder words
    facets = deck.get("facets_en") if isinstance(deck.get("facets_en"), dict) else {}
    mech = str(facets.get("mechanism") or "").strip()
    chip_text = {}
    for k in calib.YES_CHIPS:
        en = calib.YES_CHIP_TEXT_EN[k].format(mechanism=mech) if mech else \
            calib.YES_CHIP_TEXT_EN[k].replace(" ({mechanism})", "")
        chip_text[k] = {"zh": zh_yes.get(k) or k, "en": en}
    for k in calib.NO_CHIPS:
        if k in zh_no:
            chip_text[k] = {"zh": zh_no[k], "en": calib.NO_CHIP_TEXT_EN.get(k) or calib.g_chip_en(facets)}
    out = []
    for c in (deck.get("cards") or [])[:MAX_CARDS]:
        q = c.get("quote") or {}
        w = c.get("what") or {}
        n = int(c["n"])
        profile = q.get("source") == "公司简介"
        src = "profile" if profile else (l10n.source_key(q.get("source")) or q.get("source"))
        date = str(q.get("filing_date") or "")[:10] or None
        out.append({"n": n, "name": _clean_name(c.get("name")), "ticker": _ticker(c.get("security_id")),
                    "security_id": c.get("security_id"), "rank": c.get("rank"),
                    "why_zh": c.get("why_zh"), "why_en": _card_why_en(c, max_out, facets),
                    "what": _clip(w.get("text"), 200),
                    "quote": {"text": _clip(q.get("text"), QUOTE_CHARS), "profile": profile, "source": src,
                              "where": l10n.source_form_words(src, None if profile else q.get("form"), lang),
                              "date": date, "date_text": None if profile else l10n.date_words(date, lang),
                              "url": _safe_url(q.get("url"))},
                    "edge": bool((c.get("verdict") or {}).get("edge")), "tokens": tokens.get(n, {})})
    return out, chip_text


def _en_count(n: int) -> dict[str, str]:
    """The English words that agree with a count ('1 company has' / '2 companies have')."""
    one = n == 1
    return {"companies_have": "company has" if one else "companies have",
            "companies_were": "company was" if one else "companies were", "them": "it" if one else "them"}


def _gap_lines(result: dict[str, Any], country_of: dict[str, str], extra: dict[str, Any]) -> list[dict[str, Any]]:
    """Gap lines with their command (both languages), largest first."""
    gaps = result.get("gaps") or {}
    out: list[dict[str, Any]] = []
    floor = (result.get("params") or {}).get("min_mcap_usd") or 1e9

    def country(sid: str | None) -> str:
        c = country_of.get(str(sid))
        if c:
            return c
        ex = str(sid or "").split(":", 1)[0]
        return "China" if ex in CN_EXCHANGES else "?"

    nod = gaps.get("no_description") or []
    if nod:
        by: dict[str, int] = {}
        for g in nod:
            k = country(g.get("security_id"))
            by[k] = by.get(k, 0) + 1
        top = sorted(by.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        cn = by.get("China", 0)
        by_zh = "、".join(f"{'其他' if k == '?' else COUNTRY_ZH.get(k, k)} {v}" for k, v in top)
        by_en = ", ".join(f"{'other' if k == '?' else k} {v}" for k, v in top)
        cmd = f"jevscreen crawl-descriptions --countries CN --min-mcap {floor:.0e}".replace("e+0", "e")
        m = max(1, math.ceil(cn / CRAWL_REQ_PER_S / 60)) if cn else 0
        if cn and not extra.get("crawl_hidden"):     # not after the human's no to the fill, nor in a cooldown
            out.append({"id": "no_description", "count": len(nod), "cn": cn, "minutes": m, "command": cmd,
                        "text_zh": STRINGS["zh"]["gap_no_description"].format(n=len(nod), by=by_zh, cmd=cmd, m=m),
                        "text_en": STRINGS["en"]["gap_no_description"].format(n=len(nod), by=by_en, cmd=cmd, m=m,
                                                                                 **_en_count(len(nod)))})
        else:
            out.append({"id": "no_description", "count": len(nod), "cn": cn, "minutes": None, "command": None,
                        "text_zh": STRINGS["zh"]["gap_no_description_other"].format(n=len(nod), by=by_zh),
                        "text_en": STRINGS["en"]["gap_no_description_other"].format(n=len(nod), by=by_en,
                                                                                       **_en_count(len(nod)))})
    prof = gaps.get("l2_profile_only") or []
    us = [g for g in prof if country(g.get("security_id")) == "United States"
          or str(g.get("security_id") or "").split(":", 1)[0] in ("NASDAQ", "NYSE", "AMEX", "NYSEARCA")]
    if us and not extra.get("has_sec_key") and extra.get("sec_declined"):
        # the human said no to the SEC contact: the gap is stated, never pushed again
        out.append({"id": "us_profile_only", "count": len(us), "command": None, "declined": True,
                    "text_zh": STRINGS["zh"]["gap_us_profile_no"].format(k=len(us)),
                    "text_en": STRINGS["en"]["gap_us_profile_no"].format(k=len(us), **_en_count(len(us)))})
    elif us and not extra.get("has_sec_key"):
        cmd = "jevscreen keys set sec-email"
        out.append({"id": "us_profile_only", "count": len(us), "command": cmd,
                    "text_zh": STRINGS["zh"]["gap_us_profile"].format(k=len(us), cmd=cmd),
                    "text_en": STRINGS["en"]["gap_us_profile"].format(k=len(us), cmd=cmd, **_en_count(len(us)))})
    jp = [g for g in prof if country(g.get("security_id")) == "Japan" or str(g.get("security_id") or "")
          .startswith("TSE:")]
    if jp:
        out.append({"id": "jp_profile_only", "count": len(jp), "command": None,
                    "text_zh": STRINGS["zh"]["gap_jp_profile"].format(k=len(jp)),
                    "text_en": STRINGS["en"]["gap_jp_profile"].format(k=len(jp), **_en_count(len(jp)))})
    od = extra.get("ondemand") or {}
    thin = [t for t in od.get("thin_waiting") or [] if isinstance(t, dict)]
    if thin:          # novice #4 P0-1: a company a thin profile keeps out is named, it never silently disappears
        sec = any(t.get("reason") == "no_key_sec" for t in thin) and not extra.get("has_sec_key")
        k = ("gap_thin_sec_no" if extra.get("sec_declined") else "gap_thin_sec") if sec else None
        out.insert(0, {"id": "thin_waiting", "count": len(thin), "command": None,
                       "names": [t.get("security_id") for t in thin],
                       **{f"text_{lg}": STRINGS[lg]["gap_thin"].format(
                           names=("、" if lg == "zh" else ", ").join(str(t.get("name") or t.get("security_id"))
                                                                    for t in thin),
                           why=STRINGS[lg][k] if k else "") for lg in ("zh", "en")}})
    n_od = sum((od.get("fetched") or {}).values()) if isinstance(od.get("fetched"), dict) else 0
    if n_od:
        keys = sorted(k for k, v in od["fetched"].items() if v)
        src_zh = "、".join(l10n.source_words(k, "zh") or k.upper() for k in keys)
        src_en = ", ".join(l10n.source_words(k, "en") or k.upper() for k in keys)
        out.append({"id": "ondemand", "count": n_od, "command": None,
                    "text_zh": STRINGS["zh"]["gap_ondemand"].format(n=n_od, src=src_zh),
                    "text_en": STRINGS["en"]["gap_ondemand"].format(n=n_od, src=src_en)})
    return out


# --------------------------------------------------------------------------------------------------- data

def build_page_data(result: dict[str, Any], deck: dict[str, Any] | None, *, lang: str = "zh",
                    lineage: dict[str, Any] | None = None, descriptions: dict[str, str] | None = None,
                    country_of: dict[str, str] | None = None, extra: dict[str, Any] | None = None,
                    local_names: dict[str, str] | None = None,
                    l2_pieces: dict[str, dict[str, Any]] | None = None,
                    translations: dict[str, str] | None = None) -> dict[str, Any]:
    """The page's data (format jevscreen.page/1): everything the page shows, nothing it does not, in ONE language
    (`lang`: a zh page is fully Chinese, an en page fully English). `descriptions` maps company_key -> description
    text (the one-line 'what it does'); `country_of` security_id -> country (gap lines); `lineage` {'version',
    'prev_run_id', 'answers'}; `extra` {'ondemand': layers.fetch, 'has_sec_key': bool}; `local_names` security_id ->
    the official Chinese short name (CNINFO 简称, MOPS 公司簡稱), shown on a Chinese page; `l2_pieces` company_key ->
    the text layer 2 read (load_l2_pieces; default: the run folder's l2_inputs.jsonl); `translations` sha -> the
    user's agent's translation into `lang` of a text in another language (jevscreen.translations)."""
    from . import __version__, translations as tr_mod
    lang = "en" if lang == "en" else "zh"
    descriptions = descriptions or {}
    local_names = local_names or {}
    extra = extra or {}
    params = result.get("params") or {}
    today = None
    terms = idea_terms(result)
    targets, products = _verdict_terms(result)
    if l2_pieces is None:
        l2_pieces = load_l2_pieces(result.get("output_dir"), [r.get("company_key") for r in result.get("rows") or []])
    if result.get("shortlist"):
        # the confirmed list never holds a row this page marks borderline (shortlist.settle: a run written before
        # that rule is split the same way here, so the page and the chat's top agree with the tier logic)
        from . import shortlist
        result = shortlist.settle(copy.deepcopy(dict(result)), l2_pieces)
    rows_in = list(result.get("rows") or [])[:MAX_ROWS]
    rows = [_row(r, _first_sentence(descriptions.get(r.get("company_key") or "")), today, terms,
                 local_names.get(r.get("security_id") or ""), l2_pieces.get(r.get("company_key") or ""), lang)
            for r in rows_in]
    # companies that entered after a fill / re-rank and your AI has not checked yet (review.json 'unchecked'):
    # marked 未核对 / not yet checked until it has
    unchecked = set(extra.get("unchecked") or [])
    for r_in, row in zip(rows_in, rows):
        # what it does: the annual report's own business sentence (the text this row was checked on) over the
        # profile's first sentence, which can name only a side business (novice #3: 'magnetic materials')
        l2_row = l2_pieces.get(r_in.get("company_key") or "") or {}
        ar_line = business_sentence(l2_row.get("overview"), names=[
            r_in.get("name_zh"), (local_names or {}).get(r_in.get("security_id") or ""), r_in.get("name")]) if (
            r_in.get("l2_evidence") == "annual_report" and l2_row.get("sha")
            and l2_row.get("sha") == r_in.get("evidence_sha")) else None
        if ar_line:
            if row.get("one_line") and row["one_line"] != ar_line:
                row["one_line_profile"] = row["one_line"]     # still shown, second and labelled as the profile's
            row["one_line"], row["one_line_source"] = ar_line, "annual_report"
        else:
            row["one_line_source"] = "profile" if row.get("one_line") else None
        if r_in.get("company_key") in unchecked and not row["user"]:
            row["unchecked"] = True
            row["badges"] = ["unchecked"] + [b for b in row["badges"] if b != "unchecked"]
        profile = descriptions.get(r_in.get("company_key") or "")
        l2 = l2_pieces.get(r_in.get("company_key") or "")
        target_piece = _report_fills_profile_target(r_in, profile, l2, targets, products)
        if target_piece:
            prior = row["badges"]
            row["badges"] = [b for b in prior if b not in BADGE_ORDER] + \
                [b for b in BADGE_ORDER if b in prior or b == "profile_gap_report_target"]
            if row["quote"] and not mentions_idea(row["quote"]["text"], targets):
                hits = mentions_idea(target_piece, targets) or []
                row["quote"]["text"] = _window(target_piece, _hit_at(target_piece, hits))
                row["quote"]["mentions"] = True
        cited = None
        if (not row["user"] and not row.get("unchecked") and not row["scope_demoted"] and row["quote"]
                and row.get("constraint") not in ("missing", "unclear") and row.get("second") != "raised"):
            cited = _cited_direct_match(r_in, l2, targets, products)
        if cited:
            row["verdict"] = "explicit"
            row["quote"]["text"] = cited
            row["quote"]["mentions"] = True
            row["badges"] = [b for b in row["badges"] if b != "no_mention"]
            row["edge"] = "edge" in row["badges"]
        elif row["edge"] or row["scope_demoted"]:
            row["verdict"] = "edge"
    for row in rows:              # the confirmed list never shows a borderline or gap verdict (in_main)
        if row.get("tier") == "high" and not in_main(row):
            row["tier"], row["tier_why"] = "confirm", "gap"
            if row.get("main_via") == "agent":        # your AI's promotion does not outrank the shown evidence
                row["main_via"] = None
    unv = [{"name": _clean_name(r.get("name")) or r.get("security_id"), "ticker": _ticker(r.get("security_id")),
            "name_zh": local_names.get(r.get("security_id") or ""),
            "country": r.get("country"), "country_zh": COUNTRY_ZH.get(r.get("country") or ""),
            "country_text": l10n.country_words(r.get("country"), lang), "status": r.get("l2_status") or "not_run"}
           for r in list(result.get("unverified") or [])[:MAX_UNVERIFIED]]
    exc = [{"name": _clean_name(r.get("name")) or r.get("security_id"), "ticker": _ticker(r.get("security_id")),
            "name_zh": local_names.get(r.get("security_id") or ""), "note": r.get("user_note")}
           for r in result.get("excluded_by_user") or []]
    removed = _removed(result, local_names, lang)
    cards, chip_text = _cards(deck, params.get("max_out"), lang)
    for c in cards:
        c["name_zh"] = local_names.get(c.get("security_id") or "")
    if lang == "en":                  # an English page shows English names only
        for x in rows + unv + exc + cards + removed:
            x["name_zh"] = None
    f = result.get("funnel") or {}
    lin = lineage or {}
    totals = extra.get("totals") if isinstance(extra.get("totals"), dict) else None
    cost = totals["cost_usd"] if totals and totals.get("cost_usd") is not None else result.get("cost_usd")
    seconds = totals["seconds"] if totals and totals.get("seconds") else (result.get("timing") or {}).get("total_s")
    date = str(result.get("finished_at") or result.get("started_at") or "")[:10]
    idea, idea_en = result.get("idea"), result.get("idea_en")
    # the headline in the page's language: an English page of a non-English idea leads with the English sentence
    head_en = lang == "en" and bool(idea_en) and l10n.text_lang(idea) not in (None, "en")
    data = {
        "format": PAGE_FORMAT, "lang": lang, "idea": idea, "idea_en": idea_en,
        "headline": idea_en if head_en else idea, "headline_is_idea_en": head_en,
        "run_id": result.get("run_id"), "deck_id": (deck or {}).get("deck_id"), "version_tool": __version__,
        "version": int(lin.get("version") or 1), "prev_run_id": lin.get("prev_run_id"),
        "answers": int(lin.get("answers") or 0),
        "date": date, "date_text": l10n.date_words(date, lang) or "",
        "status": result.get("status"), "cost_usd": cost,
        "cost_cny": None if cost is None else round(float(cost) * CNY_PER_USD, 2),
        "seconds": seconds, "totals": bool(totals), "change": change_of(result, lin),
        "funnel": {"universe": f.get("universe"), "described": f.get("described"), "l1": f.get("l1_pass"),
                   "listed": len(result.get("rows") or []), "floor": params.get("min_mcap_usd"),
                   "rescued": sum(1 for r in result.get("rows") or [] if r.get("l1_rescued")),
                   "floor_text": _floor_text(params.get("min_mcap_usd"), lang)},
        "rows": rows, "unverified": unv, "unverified_total": len(result.get("unverified") or []),
        "unverified_groups": _unverified_groups(result.get("unverified") or []),
        "excluded": exc, "removed": removed,
        # a scope answer of the human removed some: the title names both; else only your AI's check removed them
        "removed_by_answers": any(x.get("by_answer") for x in removed), "cards": cards, "chips": chip_text, "idea_terms": idea_words_shown(result, lang), "top_n": TOP_TABLE,
        "gaps": _gap_lines(result, country_of or {}, extra),
        "strings": {lang: STRINGS[lang]}, "idea_key": _idea_key(idea),
        # the one page's upper parts: prerequisites + progress (jevscreen.pagestatus; set by page_data / write),
        # and the slot for scope questions (hidden until it has items)
        "live": extra.get("live"), "questions": extra.get("questions") or {"items": []},
        # the main list (tier high) and the to-confirm section (tier confirm): a result with the sections
        # (result['shortlist'], or rows that carry a tier); None for a single list (rows without a tier)
        "shortlist": ({"high": sum(1 for r in rows if r.get("tier") == "high"),
                       "confirm": sum(1 for r in rows if r.get("tier") != "high"),
                       "agent": sum(1 for r in rows if r.get("tier") == "high" and r.get("main_via") == "agent"),
                       **({"judge": True} if isinstance(result.get("shortlist"), dict)
                             and result["shortlist"].get("by") == "judge" else {})}
                      if isinstance(result.get("shortlist"), dict) or any(r.get("tier") for r in rows) else None),
    }
    return tr_mod.apply(data, translations)


def _removed(result: dict[str, Any], local_names: dict[str, str], lang: str) -> list[dict[str, Any]]:
    """The companies a scope answer (or the idea's own words) or the user's AI removed, each with its reason in the
    page's language (an inference: the AI read the excerpt)."""
    from . import scope
    S = STRINGS[lang]
    sv = {"facets": (result.get("scope") or {}).get("facets"), "facets_zh": (result.get("scope") or {}).get(
        "facets_zh")}
    out = []
    for r in result.get("excluded_by_scope") or []:
        kind = scope.kind_words(r.get("scope_value") or "", sv, lang)
        key = "why_default" if r.get("scope_source") == "idea_wording" else "why_scope_agent" \
            if r.get("scope_by") == "agent" else "why_scope"
        p = r.get("scope_p")
        out.append({"name": _clean_name(r.get("name")) or r.get("security_id"), "ticker": _ticker(r.get("security_id")),
                    "name_zh": local_names.get(r.get("security_id") or ""), "kind": "scope",
                    "by_answer": r.get("scope_source") != "idea_wording",
                    "why": S[key].format(kind=kind, p=f"{float(p):.0%}" if p is not None else "?")})
    for r in result.get("excluded_by_agent") or []:
        why = r.get(f"agent_why_{lang}") or ""
        out.append({"name": _clean_name(r.get("name")) or r.get("security_id"), "ticker": _ticker(r.get("security_id")),
                    "name_zh": local_names.get(r.get("security_id") or ""), "kind": "agent",
                    "why": S["why_agent"].format(why=why) if why else S["why_agent0"]})
    return out


def removed_title_key(data: dict[str, Any]) -> str:
    """The removed section's title: 'by your scope answers and your AI' only when a scope answer of the human removed
    one of them; otherwise (no scope question was asked or answered) 'removed after your AI's check'."""
    return "removed_title" if data.get("removed_by_answers") else "removed_title_agent"


def _idea_key(idea: str | None) -> str | None:
    if not idea:
        return None
    from . import keywords
    return keywords.idea_key(str(idea))


def shell_data(idea: str, *, lang: str = "zh", idea_en: str | None = None,
               status: dict[str, Any] | None = None) -> dict[str, Any]:
    """The page data of an idea without a result yet (the quickstart's first steps): the idea, the strings of its
    one language, the status block; no rows."""
    from . import __version__
    lang = "en" if lang == "en" else "zh"
    head_en = lang == "en" and bool(idea_en) and l10n.text_lang(idea) not in (None, "en")
    return {"format": PAGE_FORMAT, "lang": lang, "idea": idea, "idea_en": idea_en,
            "headline": idea_en if head_en else idea, "headline_is_idea_en": head_en, "run_id": None,
            "version_tool": __version__, "rows": [], "unverified": [], "unverified_groups": [], "excluded": [],
            "cards": [], "chips": {}, "gaps": [], "top_n": TOP_TABLE, "strings": {lang: STRINGS[lang]},
            "idea_key": _idea_key(idea), "live": status, "questions": {"items": []}}


def result_facts(data: dict[str, Any]) -> dict[str, Any]:
    """What the status block needs from a page's result: its run, status and funnel (with the sections, 'listed' is
    the confirmed list only: the scene's final sieve and its amber grains never count the to-confirm section)."""
    funnel = dict(data.get("funnel") or {})
    if data.get("shortlist"):
        funnel["listed"] = len(main_and_confirm(data)[0])
    return {"run_id": data.get("run_id"), "status": data.get("status"), "funnel": funnel}


def _status_key(status: str | None) -> str:
    return STATUS_KEYS.get(status or "", "st_other")


def _unverified_groups(unverified: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """[{key, n}] of the unverified rows grouped by reason (st_* string key), largest first."""
    by: dict[str, int] = {}
    for r in unverified:
        k = _status_key(r.get("l2_status"))
        by[k] = by.get(k, 0) + 1
    return [{"key": k, "n": n} for k, n in sorted(by.items(), key=lambda kv: (-kv[1], kv[0]))]


def display_name(row: dict[str, Any], lang: str) -> str:
    """The name a reader of `lang` recognises: on a Chinese page the official Chinese short name, else the agent's
    Chinese translation of the name, else the English name; on an English page the English name."""
    if lang == "zh":
        if row.get("name_zh"):
            return str(row["name_zh"])
        if row.get("name_tr"):
            return str(row["name_tr"])
    return str(row.get("name") or row.get("ticker") or "?")


def shown_text(holder: dict[str, Any] | None, field: str) -> str | None:
    """What the page shows for a text: its translation when there is one, else the text itself."""
    if not holder:
        return None
    return holder.get(f"{field}_tr") or holder.get(field)


def agent_words(r: dict[str, Any], lang: str) -> str | None:
    """'你的 AI 判断：拿不准——why · 等你决定' of a row your AI answered (None when it did not)."""
    a = r.get("agent") or {}
    if a.get("v") not in ("yes", "no", "unsure"):
        return None
    S = STRINGS[lang]
    zh = lang == "zh"
    why = a.get("why")
    text = S["agent_tag"] + ("：" if zh else ": ") + S[f"agent_{a['v']}"] + (
        (("——" if zh else ": ") + str(why)) if why else "")
    state = {"escalated": "agent_wait", "not_applied": "agent_not_applied"}.get(a.get("state") or "")
    return text + (" · " + S[state] if state else "")


def main_and_confirm(data: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(the main list, the to-confirm section) of a page's rows; a single list (no data['shortlist']) is all main.
    A row without a tier on a page with the sections is to confirm (never in the main list by default)."""
    rows = (data or {}).get("rows") or []
    if not (data or {}).get("shortlist"):
        return list(rows), []
    return [r for r in rows if in_main(r)], [r for r in rows if not in_main(r)]


def row_gap_shown(r: dict[str, Any]) -> bool:
    """The row's own evidence on the page does not carry the call: its verdict is 边缘 / Borderline, or the excerpt
    it shows does not mention the idea (quote.mentions false: the page says 未提到（缺口）/ gap)."""
    return r.get("verdict") == "edge" or ((r.get("quote") or {}).get("mentions") is False)


def in_main(r: dict[str, Any]) -> bool:
    """A row of the confirmed list: tier 'high' whose shown evidence supports the call. A high row whose excerpt does
    not mention the idea (or that the page calls borderline) is to confirm, whatever the tier logic said; only the
    human's own yes (their answer or pin) keeps it in the list. The page's script mirrors this (inMain)."""
    if r.get("tier") != "high":
        return False
    return bool(r.get("user") or r.get("main_via") == "user") or not row_gap_shown(r)


def split_line(data: dict[str, Any], lang: str) -> str:
    """'确认 3 家；另有 7 家待核对。' (plus 'the list may still change: why' when the run did not finish or the check did
    not read every company): the calm count line of the page, its text and the chat."""
    from . import short_list
    S = STRINGS[lang]
    main, confirm = main_and_confirm(data)
    t = S["split_line" if confirm else "split_line0"].format(m=len(main), u=len(confirm))
    if not short_list.complete_of(data):
        k = short_list.unchecked_of(data)
        t += ("" if lang == "zh" else " ") + S["split_open"].format(
            why=S["short_why_unchecked_split"].format(k=k) if k else S["short_why_run"])
    return t


def verdict_key(r: dict[str, Any], split: bool) -> str:
    """The STRINGS key of a row's verdict word: a row of the confirmed list the page could not mark 'clearly fits'
    from a cited sentence says 原文写明 / Stated (the section's note says once that this is the AI's reading), never
    相关 / Related (the word of the to-confirm rows); a row listed by the human's answer keeps its own word."""
    v = r.get("verdict") or "partial"
    if split and in_main(r) and v == "partial" and not r.get("user"):
        return "verdict_main"
    return f"verdict_{v}"


def render_text(data: dict[str, Any], lang: str | None = None) -> str:
    """The page as plain text (the <noscript> block and `jevscreen page --text`), in the page's one language:
    header, the ranked list with the verdict, the evidence kind and the one-line description (the AI translation
    when there is one, marked), the unverified reasons with counts, the gaps."""
    lang = lang or data.get("lang") or "zh"
    lang = "en" if lang == "en" else "zh"
    S = STRINGS[lang]
    zh = lang == "zh"
    sep = "：" if zh else ": "
    out = [f"{S['title']} · {data.get('headline') or data.get('idea') or ''}"]
    from . import pagestatus
    st_lines = pagestatus.text_lines(data.get("live"), lang)
    if st_lines:
        out += st_lines + [""]
    if not data.get("run_id"):
        out.append(S["results_wait"] if (data.get("live") or {}).get("refresh") else S["results_none"])
        text = "\n".join(out).rstrip() + "\n"
        return text.replace("\u2028", " ").replace("\u2029", " ")
    if data.get("version", 1) > 1:
        change = data.get("change") if isinstance(data.get("change"), dict) else None
        if change and change.get(lang):                  # what changed (the page header says the same)
            out.append(S["version_change"].format(n=data["version"], change=change[lang],
                                                  prev=data.get("prev_run_id") or "?"))
        else:
            key = "version_n" if data.get("answers") else "version_n0"
            out.append(S[key].format(n=data["version"], k=data.get("answers"), prev=data.get("prev_run_id") or "?"))
    f = data.get("funnel") or {}
    sl = data.get("shortlist") if isinstance(data.get("shortlist"), dict) else None
    out.append(S["funnel_split" if sl else "funnel"].format(
        universe=f.get("universe"), floor=_floor_text(f.get("floor"), lang), described=f.get("described"),
        l1=f.get("l1"), listed=f.get("listed"), main=(sl or {}).get("high"), confirm=(sl or {}).get("confirm")))
    if f.get("rescued"):
        out.append(S["funnel_rescued_split" if sl else "funnel_rescued"].format(n=f["rescued"]))
    if sl:
        out.append(split_line(data, lang))
    elif data.get("rows") and len(data["rows"]) < SHORT_LIST:
        from . import short_list
        u, k = int(data.get("unverified_total") or 0), short_list.unchecked_of(data)
        if short_list.complete_of(data):
            out.append(S["short_note" if u else "short_note0"].format(n=len(data["rows"]), u=u))
        else:              # the check did not read every company, or the run did not finish: the list may grow
            why = S["short_why_unchecked"].format(k=k) if k else S["short_why_run"]
            out.append(S["short_note_open"].format(n=len(data["rows"]), why=why))
    out.append(S["rank_note"])
    out.append("")
    rows = data.get("rows") or []
    main, confirm = main_and_confirm(data)
    if sl:
        out.append(S["main_title"].format(n=len(main)))
        if main:
            a = sum(1 for r in main if r.get("main_via") == "agent")
            if a and a == len(main):
                out.append(S["main_note_agent_all"].format(n=a))
            else:
                out.append(S["main_note_judge" if sl.get("judge") else "main_note"])
                if a:
                    out.append(S["main_note_agent"].format(n=a))
        else:
            from . import short_list
            key = "main_empty" if confirm else ("main_empty0" if short_list.complete_of(data) else "main_empty_open")
            out.append(S[key].format(u=len(confirm)))
    else:
        out.append(S["list_title"].format(n=len(rows)))
        if not rows:
            out.append(S["list_empty"])
    for i, r in enumerate(main + confirm):
        if sl and i == len(main):             # the to-confirm section: apart, never numbered with the main list
            out.append("")
            out.append(S["confirm_title"].format(n=len(confirm)))
            out.append(S["confirm_note"])
        name = display_name(r, lang)
        # a translated name keeps the original name beside it (small on the page)
        other = r.get("name") if zh and not r.get("name_zh") and r.get("name_tr") else None
        country = l10n.country_words(r.get("country"), lang)
        where = " · ".join(x for x in (r.get("ticker"), country) if x)
        verdict = S.get(verdict_key(r, bool(sl)), r.get("verdict") or "")
        q = r.get("quote") or {}
        if r.get("user_only"):
            ev = S["evidence_user"]
        elif q.get("mentions") is False:
            ev = S["quote_gap_profile" if r.get("evidence") == "profile" else "quote_gap"]
        else:
            ev = S.get(f"evidence_{r.get('evidence') or 'profile'}", "")
        # the evidence words already name the excerpt gap: its badge is not said a second time
        said = {"no_mention"} if q.get("mentions") is False and not r.get("user_only") else set()
        marks = ("；" if zh else "; ").join([S.get(f"badge_{b}", b) for b in r.get("badges") or [] if b not in said]
                                           + ([S["badge_user"]] if r.get("user") else []))
        to_confirm = bool(sl) and not in_main(r)
        line = (f"- {name}" if to_confirm else f"#{r.get('rank')} {name}") + (f"（{other}）" if other else "")
        via = S["via_" + r["main_via"]] if r.get("main_via") in ("agent", "agent_no") else None
        out.append(f"{line} {where} — {verdict}" + (f" · {via}" if via else "") + f" · {ev}"
                   + (f" [{marks}]" if marks else ""))
        if agent_words(r, lang):             # the why of your AI's call (the via word above says the move)
            out.append(f"    {agent_words(r, lang)}")
        if r.get("one_line"):
            one = shown_text(r, "one_line")
            tag = S["text_ai"] if r.get("one_line_tr") else (S["text_untranslated"] if r.get("one_line_x") else "")
            what = S["what_ar"] if r.get("one_line_source") == "annual_report" else S["what"]
            out.append(f"    {what}{sep}{one}{tag}")
        if r.get("one_line_profile"):
            one = shown_text(r, "one_line_profile")
            tag = S["text_ai"] if r.get("one_line_profile_tr") else (
                S["text_untranslated"] if r.get("one_line_profile_x") else "")
            out.append(f"    {S['what_profile']}{sep}{one}{tag}")
    q = data.get("questions") or {}
    if q.get("defaults"):
        out.append("")
        out.append(S["sec_defaults"])
        for d in q["defaults"]:
            out.append("  " + str(d.get("text")))
            out.append("     " + S["undo_intro"] + " " + str(d.get("undo")))
    notes = [] if q.get("defaults") else q.get("notes") or []
    if q.get("items") or notes:
        out.append("")
        out.append(S["sec_questions"])
        for n in notes:
            out.append("  " + str(n))
        for i, it in enumerate(q.get("items") or [], 1):
            out.append(f"  {i}. {it.get('text')}")
            if it.get("note"):
                out.append(f"     {it['note']}")
            out.append("     " + " / ".join(f"{o['label']} = {o['value']}" for o in it.get("options") or []))
        if q.get("template"):
            out.append("  " + S["q_line"] + " " + str(q["template"]))
    removed = data.get("removed") or []
    if removed:
        out.append("")
        out.append(S[removed_title_key(data)].format(n=len(removed)))
        for x in removed:
            out.append(f"  {display_name(x, lang)} {x.get('ticker') or ''} — {x.get('why')}")
    groups = data.get("unverified_groups") or []
    if groups:
        out.append("")
        out.append(S["unverified_title_split" if sl else "unverified_title"].format(n=data.get("unverified_total")))
        for g in groups:
            out.append("  " + S["unverified_group"].format(n=g["n"], why=S.get(g["key"], g["key"])))
    if data.get("gaps"):
        out.append("")
        out.append(S["gaps_title"])
        for g in data["gaps"]:
            out.append("  - " + str(g.get("text_zh") if zh else g.get("text_en")))
    text = "\n".join(out).rstrip() + "\n"
    return text.replace("\u2028", " ").replace("\u2029", " ")


# --------------------------------------------------------------------------------------------------- render

CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a64;--line:#e3e0d8;--card:#ffffff;--accent:#1f5f8b;--fact:#2f6b3a;
--infer:#7a4b12;--gap:#8a2d2d;--user:#4a3a8a;--badge:#f3ede0;--on:#ffffff;--ok:#2e7d40;--bad:#b3261e;
--badbg:#fdf0ee;--track:#ebe8e0}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--fg:#ecebe6;--muted:#a3a198;--line:#34332f;--card:#1f1f1c;
--accent:#7fb6de;--fact:#8fcf9a;--infer:#e2b36d;--gap:#ef9a9a;--user:#b9aef2;--badge:#2b2a26;--on:#111110;
--ok:#7fcb8e;--bad:#f28b82;--badbg:#2c1a18;--track:#34332f}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",
"PingFang SC","Hiragino Sans GB","Microsoft YaHei","Noto Sans CJK SC","Noto Sans SC",sans-serif}
main{max-width:980px;margin:0 auto;padding:16px}
h1{font-size:1.35rem;margin:.1rem 0 .5rem;line-height:1.35}h2{font-size:1.08rem;margin:0 0 .5rem}
.muted{color:var(--muted)}.small{font-size:.88rem}
button{font:inherit;min-height:44px;min-width:44px;padding:6px 14px;border-radius:10px;border:1px solid var(--line);
background:var(--card);color:var(--fg);cursor:pointer}
button.on{background:var(--accent);color:var(--on);border-color:var(--accent)}
button:disabled{opacity:.5;cursor:default}
.pillrow{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;margin:0 0 4px}
.pill{display:inline-flex;align-items:center;gap:8px;font-size:.9rem;font-weight:600;border-radius:999px;
padding:3px 12px;border:1px solid var(--accent);color:var(--accent);background:var(--card)}
.pill.p-done{border-color:var(--ok);color:var(--ok)}.pill.p-bad{border-color:var(--bad);color:var(--bad)}
.pill.p-idle{border-color:var(--line);color:var(--muted)}
section.box{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:14px 0}
section.box.secondary{background:transparent;border-style:dashed}section.box.secondary h2{color:var(--muted)}
section.box.secondary .rank{color:var(--muted);font-weight:normal}
.rank.tc{display:none}
p.empty{padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card)}
section.box.defaults{border:2px solid var(--infer)}section.box.defaults .qline{flex-wrap:wrap}
section.box.defaults code{font:13px/1.35 ui-monospace,Menlo,Consolas,monospace;overflow-wrap:anywhere}
p.mainhint{display:none}
.chk{display:flex;gap:10px;align-items:flex-start;padding:7px 0;border-top:1px solid var(--line)}
.chk.first{border-top:0}
.ic{flex:0 0 22px;width:22px;height:22px;border-radius:50%;display:inline-flex;align-items:center;
justify-content:center;font-size:13px;font-weight:700;line-height:1;margin-top:2px}
.ic-ok{background:var(--ok);color:var(--on)}.ic-need,.ic-fail{background:var(--bad);color:var(--on)}
.ic-wait{border:2px solid var(--line)}.ic-opt{border:2px dashed var(--line)}
.ic-run{border:3px solid var(--track);border-top-color:var(--accent);animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.ct{min-width:0;flex:1 1 auto}.fix{font-size:.9rem;color:var(--muted);margin-top:2px}
.chk-need .fix,.chk-fail .fix{color:var(--bad)}.chk-need .tx,.chk-fail .tx{font-weight:600}
.chk-wait .tx,.chk-opt .tx{color:var(--muted)}
details.okline>summary{list-style:none;display:flex;gap:10px;align-items:center;cursor:pointer}
details.okline>summary::-webkit-details-marker{display:none}
.okt{color:var(--ok);font-weight:600;flex:1 1 auto;min-width:0}
.more-link{font-size:.85rem;color:var(--muted);white-space:nowrap}
.subhead{font-size:.85rem;color:var(--muted);margin:12px 0 0}
details.opt>summary{cursor:pointer;font-size:.88rem;color:var(--muted);margin-top:10px}
.alert{border:1px solid var(--bad);background:var(--badbg);color:var(--bad);border-radius:10px;padding:9px 12px;
margin:0 0 10px}
.alert.note{border-color:var(--line);background:transparent;color:var(--fg)}
.pr{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:4px 12px;padding:8px 0;border-top:1px solid var(--line);
align-items:baseline}
.pr.first{border-top:0}.pr .lab{font-weight:600;min-width:0}.pr.s-wait .lab{color:var(--muted);font-weight:400}
.pr .num{text-align:right;color:var(--muted);font-variant-numeric:tabular-nums;font-size:.9rem}
.bar{grid-column:1/-1;height:8px;border-radius:99px;background:var(--track);overflow:hidden;position:relative}
.bar .fill{height:100%;background:var(--accent);border-radius:99px}
.pr.s-ok .bar .fill{background:var(--ok)}.pr.s-fail .lab,.pr.s-fail .num{color:var(--bad)}
.pr.s-fail .bar{background:var(--badbg);border:1px solid var(--bad)}
.bar.ind .fill{position:absolute;left:0;top:0;width:30%;animation:slide 1.3s ease-in-out infinite}
@keyframes slide{0%{left:-30%}100%{left:100%}}
.money{font-weight:600;margin-top:8px;padding-top:8px;border-top:1px solid var(--line)}
@media (prefers-reduced-motion:reduce){.ic-run{animation:none;border-color:var(--accent)}
.bar.ind .fill{animation:none;left:0;width:100%;opacity:.35}}
.q{padding:8px 0;border-top:1px solid var(--line)}.q.first{border-top:0}
.btns{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
.qline{display:flex;gap:8px;align-items:center;margin-top:10px}
.qline textarea{flex:1 1 auto;min-width:0;height:48px;font:13px/1.35 ui-monospace,Menlo,Consolas,monospace;
padding:6px 8px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg);resize:none}
.banner{border:1px solid var(--gap);color:var(--gap);border-radius:10px;padding:9px 12px;margin:12px 0}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:.88rem;margin:8px 0}
.tag{display:inline-block;font-size:.75rem;border-radius:6px;padding:0 6px;margin-right:6px;border:1px solid}
.t-fact{color:var(--fact);border-color:var(--fact)}.t-inference{color:var(--infer);border-color:var(--infer)}
.t-gap{color:var(--gap);border-color:var(--gap)}.t-user{color:var(--user);border-color:var(--user)}
.t-ai{color:var(--accent);border-color:var(--accent)}.t-orig{color:var(--muted);border-color:var(--muted)}
.orig{margin:4px 0;padding:4px 8px;border-left:3px solid var(--line);color:var(--muted)}
details.row{background:var(--card);border:1px solid var(--line);border-radius:12px;margin:8px 0}
details.row>summary{list-style:none;cursor:pointer;padding:10px 14px;display:flex;gap:4px 10px;flex-wrap:wrap;
align-items:baseline}
details.row>summary::-webkit-details-marker{display:none}
details.row>summary::after{content:"+";margin-left:auto;color:var(--muted);font-weight:700}
details.row[open]>summary::after{content:"\\2212"}
details.row .body{padding:0 14px 12px}
.rank{font-weight:700;color:var(--accent)}.name{font-weight:600}
.badge{background:var(--badge);border-radius:6px;padding:1px 8px;font-size:.82rem}
blockquote{margin:8px 0;padding:6px 10px;border-left:3px solid var(--fact);background:transparent}
blockquote.gapq{border-left-color:var(--gap)}
details.tech{margin-top:6px}details.tech>summary{cursor:pointer;color:var(--muted);font-size:.9rem}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:4px 16px;font-size:.9rem}
a{color:var(--accent)}
.clamp{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
button.more{min-height:32px;padding:2px 0;border:0;background:transparent;color:var(--accent);font-size:.88rem}
.tablewrap{max-width:100%;overflow-x:auto}
table.top10{width:100%;border-collapse:collapse;font-size:.92rem;background:var(--card);border:1px solid var(--line)}
table.top10 th,table.top10 td{text-align:left;vertical-align:top;padding:6px 8px;border-bottom:1px solid var(--line);
overflow-wrap:anywhere}
table.top10 th{color:var(--muted);font-weight:600;font-size:.82rem;overflow-wrap:normal}
table.top10 td:first-child,table.top10 th:first-child{white-space:nowrap;width:1%;overflow-wrap:normal}
table.top10 td:nth-child(3),table.top10 td:nth-child(4){word-break:keep-all;overflow-wrap:normal}
table.top10 td.t-gap{color:var(--gap)}table.top10 td.t-user{color:var(--user)}
table.top10 tr.edge td:first-child a{color:var(--gap)}
details.unv{margin:12px 0}details.unv>summary{cursor:pointer}details.unv .names{margin:0 0 10px 0}
main,blockquote,.row,section.box{overflow-wrap:anywhere}
pre.plain{white-space:pre-wrap;overflow-wrap:anywhere;font-size:14px;line-height:1.5;font-family:inherit;padding:0 16px}
footer{margin:28px 0 8px;font-size:.82rem;color:var(--muted)}
@media (max-width:520px){.pr{grid-template-columns:1fr}.pr .num{text-align:left}}
@media (max-width:720px){.grid{grid-template-columns:1fr}main{padding:12px 16px}
table.top10 td:nth-child(4),table.top10 th:nth-child(4){display:none}}
@media print{button,.qline{display:none}body{padding:0}}
"""

JS = r"""
(function(){
var D=JSON.parse(document.getElementById('data').textContent);
var L=D.lang==='en'?'en':'zh', SHORT=10, DBG=false, ST=D.live||null, KEY='jevscreen-page:'+(D.idea_key||D.idea||''), OPEN={}, A={};
try{DBG=/[?&]debug\b/.test(String(window.location.search||''));}catch(e){}
// the worker rewrites this page every few seconds; a heartbeat that got old means it was killed (sleep, crash)
try{if(ST&&ST.stale){var hb0=Date.parse(ST.stale.since);if(!isNaN(hb0)&&Date.now()-hb0>ST.stale.after_s*1000){
 ST.phase='failed';ST.phase_words=ST.stale.phase_words;ST.alerts=[{kind:'block',text:ST.stale.text}].concat(ST.alerts||[]);
 if(ST.progress)(ST.progress.items||[]).forEach(function(it){if(it.state==='run'){it.state='wait';it.eta=null;}});}}}catch(e){}
try{OPEN=JSON.parse(window.sessionStorage.getItem(KEY)||'{}')||{};}catch(e){OPEN={};}
if(!OPEN||typeof OPEN!=='object')OPEN={};
// the page reloads every 3 s while work runs: the chosen scope answers and the opened originals live in OPEN too
if(OPEN._a&&typeof OPEN._a==='object'){var qa=(D.questions||{}).items||[];qa.forEach(function(it){var v=OPEN._a[it.id];
 if((it.options||[]).some(function(o){return o.value===v;}))A[it.id]=v;});}
function save(){try{OPEN._y=Math.round(window.scrollY||0);OPEN._a=A;window.sessionStorage.setItem(KEY,JSON.stringify(OPEN));}catch(e){}}
try{window.__jevSave=save;window.__jevD=D;}catch(e){}
function S(k){var t=(D.strings[L]||{})[k];return (t===undefined||t===null)?k:t;}
function fmt(t,o){return t.replace(/\{(\w+)\}/g,function(m,k){return (o&&o[k]!==undefined&&o[k]!==null)?String(o[k]):'';});}
function E(tag,cls,text){var e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined&&text!==null)e.textContent=String(text);return e;}
function T(t){return document.createTextNode(String(t));}
function tag(kind,text){return E('span','tag t-'+kind,text||S('tag_'+kind));}
function isHttp(u){return typeof u==='string'&&/^https?:\/\//i.test(u);}
function link(u){var a=E('a',null,S('open_link'));a.href=u;a.rel='noopener noreferrer';a.target='_blank';return a;}
function money(v){return v===null||v===undefined?'?':(v>=0.1?v.toFixed(2):v.toFixed(4));}
function dur(s){if(s===null||s===undefined)return '?';s=Math.round(s);return s>=60?fmt(S('dur_ms'),{m:Math.floor(s/60),s:s%60}):fmt(S('dur_s'),{s:s});}
function ctry(o){return o.country_text||(L==='zh'?(o.country_zh||o.country):o.country)||null;}
function nm(o){return (L==='zh'&&(o.name_zh||o.name_tr))||o.name||o.ticker||'?';}
function sub(o){return (L==='zh'&&!o.name_zh&&o.name_tr)?o.name:null;}
function nmFull(o){var s=sub(o);return nm(o)+(s?'（'+s+'）':'');}
function lbl(v){if(v===null||v===undefined||v==='')return null;var t=(D.strings[L]||{})['lbl_'+v];return t||String(v);}
function pct(v){return (v===null||v===undefined||isNaN(v))?null:Math.round(Number(v)*100)+'%';}
function gapq(q){return !!q&&q.mentions===false;}
function colon(){return L==='zh'?'：':': ';}
var HASH='';try{HASH=String(window.location.hash||'').replace(/^#/,'');}catch(e){}
function fold(id,cls,dflt){var d=E('details',cls);d.id=id;var o=OPEN[id];d.open=(o===undefined)?(HASH===id||!!dflt):!!o;
 d.ontoggle=function(){OPEN[id]=d.open?1:0;save();};return d;}
function origToggle(orig,id){var w=E('div','small');var o=E('div','orig');var on=!!(id&&OPEN[id]);o.style.display=on?'block':'none';o.appendChild(tag('fact',S('orig_label')));o.appendChild(T(orig));
 var bt=E('button','more',S(on?'hide_orig':'show_orig'));bt.onclick=function(){var shut=o.style.display==='none';o.style.display=shut?'block':'none';bt.textContent=S(shut?'hide_orig':'show_orig');
  if(id){OPEN[id]=shut?1:0;save();}};
 w.appendChild(bt);w.appendChild(o);return w;}
function trLine(cls,prefix,orig,tr,foreign,id){var d=E('div',cls);if(prefix)d.appendChild(T(prefix));
 if(foreign&&tr){d.appendChild(T(tr+' '));d.appendChild(tag('ai',S('ai_tr')));d.appendChild(origToggle(orig,id));}
 else{if(foreign){d.appendChild(tag('orig',S('untranslated')));}d.appendChild(T(orig));}return d;}
function clampBox(b,tx){if(String(tx.textContent).length<=60)return;tx.className='clamp';var mb=E('button','more',S('more'));
 mb.onclick=function(){var shut=tx.className==='clamp';tx.className=shut?'':'clamp';mb.textContent=S(shut?'less':'more');};b.appendChild(mb);
 setTimeout(function(){try{if(tx.className==='clamp'&&tx.scrollHeight>0&&tx.scrollHeight<=tx.clientHeight+2)mb.style.display='none';}catch(e){}},0);}
function quote(q,profile,byUser,id){var b=E('div');
 if(!q||!q.text){b.appendChild(tag('fact'));b.appendChild(E('span','muted',S('no_quote')));return b;}
 var miss=gapq(q),pr=profile||q.profile||q.source==='profile',trd=!!(q.text_x&&q.text_tr);
 if(miss)b.appendChild(tag('gap',S(pr?'quote_gap_profile':'quote_gap')));else if(!trd)b.appendChild(tag('fact'));
 if(trd)b.appendChild(tag('ai',S('ai_tr_quote')));else if(q.text_x)b.appendChild(tag('orig',S('untranslated')));
 var bq=E('blockquote',miss?'gapq':null);var tx=E('div',null,trd?q.text_tr:q.text);bq.appendChild(tx);b.appendChild(bq);clampBox(b,tx);
 if(trd)b.appendChild(origToggle(q.text,id));
 if(miss)b.appendChild(E('div','small muted',fmt(S(byUser?'quote_gap_why_user':'quote_gap_why'),{terms:(D.idea_terms||[]).slice(0,8).join(L==='zh'?'、':', ')})));
 var src=[q.where||(pr?S('evidence_profile'):null),q.date_text].filter(Boolean).join(' · ');
 if(src||isHttp(q.url)){var m=E('div','small muted',src?S('quote_from')+colon()+src+' ':'');if(isHttp(q.url))m.appendChild(link(q.url));b.appendChild(m);}return b;}
function evCell(r){if(r.user_only)return [S('tag_user'),'t-user'];
 if(gapq(r.quote)&&!r.user)return [S(r.evidence==='profile'?'ev_short_gap_profile':'ev_short_gap'),'t-gap'];
 return [S('ev_short_'+(r.evidence||'profile'))+(r.user?' · '+S('tag_user'):''),null];}
function icon(state){var s=E('span','ic ic-'+state,state==='ok'?'✓':((state==='need'||state==='fail')?'✕':''));s.setAttribute('aria-label',S('st_'+state));s.setAttribute('role','img');return s;}
function checkRow(c,first){var r=E('div','chk chk-'+c.state+(first?' first':''));r.appendChild(icon(c.state));var b=E('div','ct');
 b.appendChild(E('div','tx',c.text));if(c.fix)b.appendChild(E('div','fix',c.fix));r.appendChild(b);return r;}
// 1) prerequisites: one green line when everything is ready, else every check with its fix
function secReady(m){if(!ST||!(ST.checks||[]).length)return;var box=E('section','box ready');
 function list(into){(ST.checks||[]).forEach(function(c,i){into.appendChild(checkRow(c,i===0));});
  var op=ST.optional||[];if(op.length){var od=fold('ready-optional','opt',false);od.appendChild(E('summary',null,fmt(S('sec_optional'),{n:op.length,k:op.filter(function(c){return c.state==='ok';}).length})));
   op.forEach(function(c){od.appendChild(checkRow(c,false));});into.appendChild(od);}}
 if(ST.all_ok){var d=fold('ready-list','okline',false);var sm=E('summary');sm.appendChild(icon('ok'));sm.appendChild(E('span','okt',ST.ok_line));
  sm.appendChild(E('span','more-link',S('show_checks')));d.appendChild(sm);var inner=E('div');inner.style.marginTop='8px';list(inner);d.appendChild(inner);box.appendChild(d);}
 else{box.appendChild(E('h2',null,S('sec_ready')));list(box);}
 m.appendChild(box);}
// 2) live progress: per data item done/total with bars, MB, ETA, money; blocks and cooldowns in red
function secProgress(m){if(!ST)return;var al=ST.alerts||[],p=ST.progress;if(!al.length&&!p)return;var box=E('section','box progress');
 box.appendChild(E('h2',null,S('sec_progress')));
 al.forEach(function(a){box.appendChild(E('div','alert'+(a.kind==='note'?' note':''),a.text));});
 if(p){(p.items||[]).forEach(function(it,i){var r=E('div','pr s-'+it.state+(i===0?' first':''));r.appendChild(E('div','lab',it.label));
   r.appendChild(E('div','num',[it.text,it.eta].filter(Boolean).join(' · ')));
   var bar=E('div','bar'+(it.state==='run'&&(it.pct===null||it.pct===undefined)?' ind':''));var f=E('div','fill');
   f.style.width=(it.state==='run'&&(it.pct===null||it.pct===undefined))?'30%':((it.pct||0)+'%');bar.appendChild(f);r.appendChild(bar);box.appendChild(r);});
  if(p.money)box.appendChild(E('div','money',p.money));
  if(p.updated||ST.refresh)box.appendChild(E('div','small muted',[p.updated,ST.refresh?S('refreshing'):null].filter(Boolean).join(' · ')));}
 m.appendChild(box);}
// 3) scope questions: hidden until the data has some; the answers build the line to paste to the AI
function qLine(q){var parts=[];(q.items||[]).forEach(function(it){if(A[it.id])parts.push(A[it.id]);});
 if(!parts.length)return '';return String(q.template||'{answers}').replace('{answers}',parts.join(' '));}
function copyBtn(get,label){var b=E('button',null,label);var msg=E('span','small muted');
 b.onclick=function(){var text=get();function ok(){b.textContent=S('copied');}
  function manual(){msg.textContent=text;try{var r=document.createRange&&document.createRange();if(r&&window.getSelection){r.selectNodeContents(msg);var sl=window.getSelection();sl.removeAllRanges();sl.addRange(r);if(document.execCommand('copy')){ok();return;}}}catch(e){}}
  if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(text).then(ok,manual);}else{manual();}};
 return [b,msg];}
// 2b) scope defaults applied from the idea's own words: shown above the results, each with a one-click undo line
function secDefaults(m){var ds=(D.questions||{}).defaults||[];if(!ds.length)return;var box=E('section','box defaults');
 box.appendChild(E('h2',null,S('sec_defaults')));
 ds.forEach(function(d){var p=E('p');p.appendChild(tag('inference'));p.appendChild(T(d.text));box.appendChild(p);
  var row=E('div','qline');row.appendChild(E('div','small muted',S('undo_intro')));row.appendChild(E('code',null,d.undo));
  var cb=copyBtn(function(){return d.undo;},S('undo_default'));row.appendChild(cb[0]);row.appendChild(cb[1]);box.appendChild(row);});
 m.appendChild(box);}
function secQuestions(m){var q=D.questions||{};var notes=(q.defaults||[]).length?[]:(q.notes||[]);if(!(q.items||[]).length&&!notes.length)return;var box=E('section','box questions');
 box.appendChild(E('h2',null,S('sec_questions')));notes.forEach(function(n){var p=E('p','small');p.appendChild(tag('inference'));p.appendChild(T(n));box.appendChild(p);});
 if(!(q.items||[]).length){m.appendChild(box);return;}box.appendChild(E('p','small muted',S('q_intro')));
 var ta=E('textarea');ta.readOnly=true;ta.rows=2;ta.setAttribute('aria-label',S('q_line'));var cp=E('button',null,S('copy'));var msg=E('span','small muted');
 function refresh(){var line=qLine(q);ta.value=line;ta.placeholder=S('q_empty');cp.disabled=!line;cp.textContent=S('copy');}
 q.items.forEach(function(it,i){var d=E('div','q'+(i===0?' first':''));d.appendChild(E('div',null,(i+1)+'. '+it.text));var bt=E('div','btns');
  if(it.note)d.appendChild(E('div','small muted',it.note));
  (it.options||[]).forEach(function(o){var b=E('button',A[it.id]===o.value?'on':null,o.label);b.onclick=function(){A[it.id]=(A[it.id]===o.value?null:o.value);
   var kids=bt.children||[];for(var k=0;k<kids.length;k++){kids[k].className=(kids[k]===b&&A[it.id])?'on':'';}refresh();save();};bt.appendChild(b);});
  d.appendChild(bt);box.appendChild(d);});
 var row=E('div','qline');row.appendChild(ta);
 cp.onclick=function(){var text=ta.value;function ok(){cp.textContent=S('copied');}
  function manual(){ta.focus();ta.select();try{if(document.execCommand('copy')){ok();return;}}catch(e){}msg.textContent=S('copy_failed');}
  if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(text).then(ok,manual);}else{manual();}};
 row.appendChild(cp);box.appendChild(E('div','small',S('q_line')));box.appendChild(row);box.appendChild(msg);refresh();m.appendChild(box);}
// 4) results: the confirmed list at a glance (every row; without the sections the top 10), then one expandable row per company with its evidence
// the confirmed list and the to-confirm section (D.shortlist; rows carry tier 'high' / 'confirm'); else one list
// the confirmed list: tier high whose shown evidence carries the call (page.in_main): never a borderline verdict or an
// excerpt that does not mention the idea, unless the human said yes
function inMain(r){if(r.tier!=='high')return false;return !!(r.user||r.main_via==='user')||!(r.verdict==='edge'||(!!r.quote&&r.quote.mentions===false));}
function mainRows(){return (D.rows||[]).filter(function(r){return !D.shortlist||inMain(r);});}
function confirmRows(){return D.shortlist?(D.rows||[]).filter(function(r){return !inMain(r);}):[];}
function vword(r){return S((D.shortlist&&inMain(r)&&(r.verdict||'partial')==='partial'&&!r.user)?'verdict_main':'verdict_'+(r.verdict||'partial'));}
function viaWord(r){return r.main_via==='agent'?S('via_agent'):(r.main_via==='agent_no'?S('via_agent_no'):null);}
function unchk(){var k=0;(D.unverified_groups||[]).forEach(function(g){if(['st_skipped_budget','st_failed','st_other'].indexOf(g.key)>=0)k+=g.n||0;});return k;}
function splitLine(){var sl=D.shortlist,k=unchk(),m=mainRows().length,u=confirmRows().length;
 var t=fmt(S(u?'split_line':'split_line0'),{m:m,u:u});
 if(!(D.status==='ok'&&!k))t+=(L==='zh'?'':' ')+fmt(S('split_open'),{why:k?fmt(S('short_why_unchecked_split'),{k:k}):S('short_why_run')});return t;}
function topTable(m){var SL=!!D.shortlist;var all=SL?mainRows():(D.rows||[]);var rows=SL?all.slice():all.slice(0,D.top_n||10);
 if(SL){m.appendChild(E('h2','mainh',fmt(S('main_title'),{n:all.length})));
  if(!rows.length){var u=confirmRows().length;m.appendChild(E('p','empty',fmt(S(u?'main_empty':(D.status==='ok'&&!unchk()?'main_empty0':'main_empty_open')),{u:u})));return;}
  var ag=all.filter(function(r){return r.main_via==='agent';}).length;
  m.appendChild(E('p','small muted mainnote',ag&&ag===all.length?fmt(S('main_note_agent_all'),{n:ag}):S(D.shortlist.judge?'main_note_judge':'main_note')+(ag?(L==='zh'?'':' ')+fmt(S('main_note_agent'),{n:ag}):'')));
  // the results panel's one line that the confirmed rows' word is the AI's reading (the plain page's note says it)
  if(all.some(function(r){return vword(r)===S('verdict_main');}))m.appendChild(E('p','small muted mainhint',S('main_hint')));}
 if(!rows.length)return;
 var grade={explicit:0,partial:1,edge:2};rows.sort(function(a,b){var x=grade[a.verdict],y=grade[b.verdict];
  return (x===undefined?1:x)-(y===undefined?1:y)||a.rank-b.rank;});
 if(!SL)m.appendChild(E('h2',null,fmt(S('top_title'),{n:rows.length})));var t=E('table','top10');var th=E('tr');
 ['col_rank','col_name','col_verdict','col_evidence'].forEach(function(k){th.appendChild(E('th',null,S(k)));});t.appendChild(th);
 rows.forEach(function(r){var tr=E('tr',r.verdict==='edge'?'edge':null);tr.setAttribute('data-rank',String(r.rank));var a=E('a',null,'#'+r.rank);a.href='#r'+r.rank;
  a.onclick=function(){var d=document.getElementById('r'+r.rank);if(d&&d.tagName&&String(d.tagName).toLowerCase()==='details'){d.open=true;OPEN['r'+r.rank]=1;save();}};
  var c0=E('td');c0.appendChild(a);tr.appendChild(c0);
  var c1=E('td');c1.appendChild(E('div','name',nm(r)));if(sub(r))c1.appendChild(E('div','small muted',sub(r)));c1.appendChild(E('div','small muted',[r.ticker,ctry(r)].filter(Boolean).join(' · ')));tr.appendChild(c1);
  tr.appendChild(E('td',null,vword(r)+(viaWord(r)?' · '+viaWord(r):'')+(r.unchecked?' · '+S('badge_unchecked'):'')+(agentShort(r)?' · '+agentShort(r):'')));
  var ev=evCell(r);tr.appendChild(E('td',ev[1],ev[0]));t.appendChild(tr);});
 var w=E('div','tablewrap');w.appendChild(t);m.appendChild(w);}
function agentLine(r){var a=r.agent;if(!a||['yes','no','unsure'].indexOf(a.v)<0)return null;var d=E('div','small');
 d.appendChild(tag('inference',S('agent_tag')));var t=S('agent_'+a.v)+(a.why?(L==='zh'?'——':': ')+a.why:'');
 var st={escalated:'agent_wait',not_applied:'agent_not_applied'}[a.state];d.appendChild(T(' '+t+(st?' · '+S(st):'')));return d;}
function agentShort(r){var a=r.agent;if(r.main_via==='agent_no')return null;return (a&&(a.v==='no'||a.v==='unsure'))?S('agent_tag')+(L==='zh'?'：':': ')+S('agent_'+a.v):null;}
function rowBox(r){var d=fold('r'+r.rank,'row',false);var sm=E('summary');var tc=!!D.shortlist&&!inMain(r);
 var rk=E('span',tc?'rank tc':'rank',tc?'':'#'+r.rank);if(tc)rk.setAttribute('aria-hidden','true');sm.appendChild(rk);sm.appendChild(E('span','name',nm(r)));
 sm.appendChild(E('span','muted small',[r.ticker,ctry(r)].filter(Boolean).join(' · ')));
 sm.appendChild(E('span','small',vword(r)));
 if(viaWord(r))sm.appendChild(E('span',r.main_via==='agent'?'badge via':'badge via-no',viaWord(r)));
 if((r.badges||[]).length)sm.appendChild(E('span','badge',S('badge_'+r.badges[0])));
 if(agentShort(r))sm.appendChild(E('span','badge',agentShort(r)));d.appendChild(sm);
 var b=E('div','body');if(sub(r))b.appendChild(E('div','small muted',sub(r)));
 if(r.one_line)b.appendChild(trLine('small',S(r.one_line_source==='annual_report'?'what_ar':'what')+colon(),r.one_line,r.one_line_tr,r.one_line_x,'o:r'+r.rank+':what'));
 if(r.one_line_profile)b.appendChild(trLine('small muted',S('what_profile')+colon(),r.one_line_profile,r.one_line_profile_tr,r.one_line_profile_x,'o:r'+r.rank+':whatp'));
 var v=E('div');v.appendChild(tag('inference'));v.appendChild(E('strong',null,vword(r)));
 v.appendChild(T(' · '+S(r.user_only?'evidence_user':'evidence_'+(r.evidence||'profile'))));
 if(r.user){v.appendChild(T(' '));v.appendChild(tag('user'));}b.appendChild(v);if(agentLine(r))b.appendChild(agentLine(r));
 b.appendChild(quote(r.quote,r.evidence==='profile',r.user,'o:r'+r.rank+':quote'));
 var det=E('details','tech');det.appendChild(E('summary',null,S('details')));var g=E('div','grid');
 function kv(k,val){if(val===null||val===undefined||val==='')return;g.appendChild(E('div','muted',S(k)));g.appendChild(E('div',null,val));}
 var rd=r.details||{};kv('reads_label',rd.reads?(rd.reads.n>1?fmt(S('reads'),rd.reads):S('reads_one')):null);
 kv('l1',lbl(rd.l1));kv('l2',lbl(rd.l2));kv('p_pos',pct(rd.p_pos));if(DBG)kv('p_core',rd.p_core);kv('mcap',r.mcap_text);
 kv('badges',(r.badges||[]).map(function(x){return S('badge_'+x);}).join(L==='zh'?'；':'; '));det.appendChild(g);b.appendChild(det);
 d.appendChild(b);return d;}
function secResults(m){var box=E('section','results');m.appendChild(box);
 if(!D.run_id){box.appendChild(E('h2',null,S('sec_results')));box.appendChild(E('p','muted',S(ST&&ST.refresh?'results_wait':'results_none')));return;}
 box.appendChild(E('h2',null,S('sec_results')));
 box.appendChild(E('div','small',D.version>1?(D.change&&D.change[L]?fmt(S('version_change'),{n:D.version,change:D.change[L],prev:D.prev_run_id||'?'}):fmt(S(D.answers?'version_n':'version_n0'),{n:D.version,k:D.answers,prev:D.prev_run_id||'?'})):S('version_first')));
 var st=S('status_'+D.status);if(st==='status_'+D.status)st=S('status_other');
 box.appendChild(E('div','small muted',fmt(S(D.totals?'meta_total':'meta'),{date:D.date_text||D.date,status:st,cost:money(D.cost_usd),
  cny:(L==='zh'&&D.cost_cny!==null&&D.cost_cny!==undefined)?fmt(S(D.cost_usd>0&&D.cost_cny<0.005?'cny_tiny':'cny'),{y:D.cost_cny.toFixed(2)}):'',time:dur(D.seconds)}).replace(/^ · /,'')));
 var f=D.funnel||{};box.appendChild(E('p',null,fmt(S(D.shortlist?'funnel_split':'funnel'),{universe:f.universe,floor:f.floor_text||'?',described:f.described,l1:f.l1,listed:f.listed,main:(D.shortlist||{}).high,confirm:(D.shortlist||{}).confirm})));
 if(f.rescued)box.appendChild(E('p',null,fmt(S(D.shortlist?'funnel_rescued_split':'funnel_rescued'),{n:f.rescued})));
 if(D.shortlist)box.appendChild(E('p','short',splitLine()));
 var nr=(D.rows||[]).length;if(!D.shortlist&&nr&&nr<SHORT){var u=D.unverified_total||0,k=0;(D.unverified_groups||[]).forEach(function(g){if(['st_skipped_budget','st_failed','st_other'].indexOf(g.key)>=0)k+=g.n||0;});
  box.appendChild(E('p','short',(D.status==='ok'&&!k)?fmt(S(u?'short_note':'short_note0'),{n:nr,u:u}):fmt(S('short_note_open'),{n:nr,why:k?fmt(S('short_why_unchecked'),{k:k}):S('short_why_run')})));}
 box.appendChild(E('div','banner',S('banner')));
 topTable(box);
 box.appendChild(E('p','small muted',S('rank_note')));
 var lg=E('div','legend');['fact','inference','gap','user'].forEach(function(k){var s=E('span');s.appendChild(tag(k));s.appendChild(T(S('legend_'+k)));lg.appendChild(s);});
 if(D.translation&&D.translation.foreign){var sa=E('span');sa.appendChild(tag('ai',S('ai_tr')));sa.appendChild(T(S('legend_ai')));lg.appendChild(sa);}
 box.appendChild(lg);
 var rows=D.rows||[];
 if(D.shortlist){var mr=mainRows(),cr=confirmRows();
  if(mr.length){box.appendChild(E('h2',null,fmt(S('main_rows_title'),{n:mr.length})));mr.forEach(function(r){box.appendChild(rowBox(r));});}
  if(cr.length){var cs=E('section','box secondary');cs.appendChild(E('h2',null,fmt(S('confirm_title'),{n:cr.length})));
   cs.appendChild(E('p','small muted',S('confirm_note')));cr.forEach(function(r){cs.appendChild(rowBox(r));});box.appendChild(cs);}}
 else if(!rows.length){box.appendChild(E('p','muted',S('list_empty')));}
 else{box.appendChild(E('h2',null,fmt(S('rows_title'),{n:rows.length})));
  rows.forEach(function(r){box.appendChild(rowBox(r));});}
 if((D.unverified||[]).length){var ud=fold('unverified','unv',false);ud.appendChild(E('summary',null,fmt(S(D.shortlist?'unverified_title_split':'unverified_title'),{n:D.unverified_total})));
  var SK={contradicted:'st_contradicted',skipped_budget:'st_skipped_budget',failed:'st_failed',uncertain:'st_failed',no_excerpt:'st_no_excerpt',insufficient:'st_insufficient'};
  var groups=D.unverified_groups||[];var byKey={};D.unverified.forEach(function(u){var k=SK[u.status]||'st_other';(byKey[k]=byKey[k]||[]).push(u);});
  if(!groups.length)groups=Object.keys(byKey).map(function(k){return {key:k,n:byKey[k].length};});
  groups.forEach(function(gr){var p=E('p','small');p.appendChild(tag('gap'));p.appendChild(T(fmt(S('unverified_group'),{n:gr.n,why:S(gr.key)})));ud.appendChild(p);
   var us=byKey[gr.key]||[];if(us.length)ud.appendChild(E('div','small muted names',us.map(function(u){return nmFull(u)+(u.ticker?' '+u.ticker:'');}).join(L==='zh'?'、':', ')+(us.length<gr.n?' …':'')));});
  box.appendChild(ud);}
 if((D.removed||[]).length){var rd=fold('removed','unv',false);rd.appendChild(E('summary',null,fmt(S(D.removed_by_answers?'removed_title':'removed_title_agent'),{n:D.removed.length})));
  D.removed.forEach(function(x){var p=E('div','small');p.appendChild(tag('inference'));p.appendChild(T(nmFull(x)+' '+(x.ticker||'')+' — '+x.why));rd.appendChild(p);});box.appendChild(rd);}
 if((D.excluded||[]).length){box.appendChild(E('h2',null,fmt(S('excluded_title'),{n:D.excluded.length})));
  D.excluded.forEach(function(x){var p=E('div','small');p.appendChild(tag('user'));p.appendChild(T(nmFull(x)+' '+(x.ticker||'')));box.appendChild(p);});}
 if((D.gaps||[]).length){box.appendChild(E('h2',null,S('gaps_title')));D.gaps.forEach(function(g){var p=E('p','small');p.appendChild(tag('gap'));
  p.appendChild(T(L==='en'?g.text_en:g.text_zh));box.appendChild(p);});}
 box.appendChild(E('footer',null,fmt(S('footer'),{run:D.run_id,deck:D.deck_id||'-',ver:D.version_tool})));}
function render(){
 document.documentElement.lang=L==='zh'?'zh-CN':'en';
 document.title=(ST&&ST.phase!=='done'&&ST.phase_words?ST.phase_words+' · ':'')+S('title')+' · '+(D.headline||D.idea||'');
 var app=document.getElementById('app');app.textContent='';var m=E('main');app.appendChild(m);
 var h=E('header');h.appendChild(E('div','muted small',S('idea')));h.appendChild(E('h1',null,D.headline||D.idea));
 var alt=D.headline_is_idea_en?[S('idea_orig'),D.idea]:((D.idea_en&&D.idea_en!==(D.headline||D.idea))?[S('idea_en'),D.idea_en]:null);
 if(alt){var ad=E('details','small muted');ad.appendChild(E('summary',null,alt[0]));ad.appendChild(E('div','orig',alt[1]));h.appendChild(ad);}
 if(ST&&ST.phase_words){var pr=E('div','pillrow');var cls={done:'p-done',blocked:'p-bad',failed:'p-bad',key:'p-bad',declined:'p-bad',wait_you:'p-bad',fresh:'p-idle',busy:'p-idle',wait_ai:'p-idle'}[ST.phase]||'';
  var pill=E('span','pill '+cls);if(ST.refresh&&['done','blocked','failed','key','declined','wait_you','fresh','busy','wait_ai'].indexOf(ST.phase)<0)pill.appendChild(icon('run'));
  pill.appendChild(T(ST.phase_words));pr.appendChild(pill);h.appendChild(pr);}
 m.appendChild(h);
 secReady(m);secProgress(m);secDefaults(m);secQuestions(m);secResults(m);
 try{if(OPEN._y)window.scrollTo(0,OPEN._y);window.onscroll=function(){if(!window.__jevT){window.__jevT=setTimeout(function(){window.__jevT=null;save();},300);}};}catch(e){}
}
render();
})();
"""


CHANGE_TEXT = {"zh": {"answers": "应用了你的 {n} 个回答后重排", "fetch": "补抓 {n} 家年报后重排",
                      "fill": "补了 {n} 家公司简介后重排", "fill_fetch": "补了 {m} 家公司简介、补抓 {n} 家年报后重排",
                      "rerun": "重新筛选", "scope": "按你的 {n} 个范围回答调整", "agent": "你的 AI 核对了 {n} 家后调整",
                      "decide": "按你的回答调整", "reapply": "补简介后按你的回答重排"},
               "en": {"answers": "re-ranked with your {n} answers", "fetch": "re-ranked after fetching {n} annual "
                      "reports", "fill": "re-ranked after filling profiles of {n} companies",
                      "fill_fetch": "re-ranked after filling profiles of {m} companies and fetching {n} annual reports",
                      "rerun": "screened again", "scope": "adjusted with your {n} scope answers",
                      "agent": "adjusted after your AI checked {n} companies", "decide": "adjusted with your answers",
                      "reapply": "re-ranked with your answers after the profile fill"}}


def change_of(result: dict[str, Any], lineage: dict[str, Any] | None = None) -> dict[str, str] | None:
    """What this version changed against the previous one, in both languages (None for a first run): the human's
    card answers, an on-demand annual-report fetch (the update pass), a profile fill (l1_new), a fill followed by
    its fetch pass (lineage prev_fill: the profiles the previous version filled) or a plain rerun."""
    params = result.get("params") or {}
    if not params.get("from_run") and not result.get("supersedes"):
        return None
    ck = params.get("change_kind")
    if ck in ("scope", "agent", "decide", "reapply"):
        # a free rank_only version (judge / decide / the fill's reapply): never 'applied your N answers'
        n = 0
        if ck == "agent":
            n = int(((result.get("layers") or {}).get("rank_only") or {}).get("reviewed") or 0)
        elif ck == "scope":
            n = len(((result.get("scope") or {}).get("enforced")) or [])
        m = 0
        return {"kind": ck, "n": n, "m": m, "zh": CHANGE_TEXT["zh"][ck].format(n=n, m=m),
                "en": CHANGE_TEXT["en"][ck].format(n=n, m=m)}
    answers = int(((result.get("calibration") or {}).get("answers")) or (lineage or {}).get("answers") or 0)
    fetched = (((result.get("layers") or {}).get("fetch") or {}).get("fetched") or {})
    if answers:
        kind, n = "answers", answers
    elif result.get("supersedes") or params.get("update_budget_usd") is not None:
        kind, n = "fetch", sum(int(v or 0) for v in fetched.values()) if isinstance(fetched, dict) else 0
    elif params.get("l1_new"):
        kind, n = "fill", int(((result.get("layers") or {}).get("l1") or {}).get("new") or 0)
    else:
        kind, n = "rerun", 0
    m = int((lineage or {}).get("prev_fill") or 0)
    if kind == "fetch" and m:
        kind = "fill_fetch"         # the fill's own version is replaced by its fetch pass: name both
    return {"kind": kind, "n": n, "m": m, "zh": CHANGE_TEXT["zh"][kind].format(n=n, m=m),
            "en": CHANGE_TEXT["en"][kind].format(n=n, m=m)}


def _filled_by(con, run_id: str) -> int:
    """How many companies a profile-fill run (l1_new) read for the first time (0 for any other run)."""
    row = con.execute("SELECT params_json FROM screen_runs WHERE run_id = ?", [run_id]).fetchone()
    try:
        params = json.loads(row[0] or "{}") if row else {}
    except ValueError:
        return 0
    base = params.get("from_run") if params.get("l1_new") else None
    if not base:
        return 0
    return int(con.execute(
        "SELECT count(*) FROM screen_results a WHERE a.run_id = ? AND a.layer = 'l1' AND NOT EXISTS (SELECT 1 FROM "
        "screen_results b WHERE b.run_id = ? AND b.layer = 'l1' AND b.company_key = a.company_key)",
        [run_id, base]).fetchone()[0] or 0)


def render_page(data: dict[str, Any]) -> str:
    """The HTML of the page for `data` (build_page_data), in the data's one language."""
    import html as _html
    from . import page_hud, page_sand   # the full-window sand scene and its HUD (inline; turned on by their script)
    lg = "en" if data.get("lang") == "en" else "zh"
    S = STRINGS[lg]
    title = _html.escape(f"{S['title']} · {data.get('headline') or data.get('idea') or ''}", quote=False)
    text = _html.escape(render_text(data), quote=False)
    # while work runs, the page reloads itself (the worker rewrites it): a JS timer that waits while someone uses the
    # page, a meta refresh without JS; both are gone once it is done
    refresh = (f'<meta name="jevscreen-refresh" content="{REFRESH_S}">\n'
               f'<noscript><meta http-equiv="refresh" content="{REFRESH_S}"></noscript>\n') \
        if (data.get("live") or {}).get("refresh") else ""
    return ("<!DOCTYPE html>\n"
            f'<html lang="{"en" if lg == "en" else "zh-CN"}">\n<head>\n<meta charset="utf-8">\n'
            f'<meta http-equiv="Content-Security-Policy" content="{CSP}">\n'
            '<meta name="referrer" content="no-referrer">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n' + refresh +
            f"<title>{title}</title>\n<style>{CSS}{page_sand.CSS}{page_hud.CSS}</style>\n</head>\n<body>\n" +
            page_hud.markup(data) + page_sand.markup(data) +
            f'<div id="app"><noscript><p>{S["no_js"]}</p><pre class="plain">{text}</pre></noscript></div>\n'
            f'<script type="application/json" id="data">{_json_for_script(data)}</script>\n'
            # the reload timer runs FIRST and the app is fenced (try): a throw while the app renders (e.g. a data shape
            # written mid-run) never stops the next reload, so the worker's next rewrite can repair the page
            # the HUD's script runs after the page's (it needs #app) and before the scene's (which asks it for the insets)
            f"<script>{REFRESH_JS}try{{{JS}}}catch(e){{try{{console.error(e);}}catch(e2){{}}}}\n{page_hud.JS}\n{page_sand.JS}"
            "</script>\n</body>\n</html>\n")


# --------------------------------------------------------------------------------------------------- store glue

def _local_names(con, sids: list[str]) -> dict[str, str]:
    """security_id -> CNINFO short name (简称, e.g. 高澜股份) of the A-share lines among `sids`, from the newest CNINFO
    stock list in the store (also one a pack imported) joined through identifiers.cninfo_orgid; {} without a list."""
    from . import names, shells
    if not sids:
        return {}
    path = names._newest_raw(con, shells.ST_SOURCE, (shells.ST_KIND,))
    if path is None:
        return {}
    # one org_id can carry several lines (南玻A 000012 and 南玻B 200012): the line's own code decides; the org_id
    # alone only when no row has the code, and then its A-share row
    by_code: dict[tuple[str, str], str] = {}
    by_org: dict[str, str] = {}
    for r in shells.read_stock_list(path):
        if not r.get("name"):
            continue
        n = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(r["name"])))
        org = str(r["org_id"])
        by_code[(org, str(r.get("code") or ""))] = n
        if org not in by_org or str(r.get("category") or "") == "A股":
            by_org[org] = n
    out = {}
    for sid, org in con.execute(
            "SELECT security_id, id_value FROM identifiers WHERE id_type = ? AND list_contains(?::VARCHAR[], "
            "security_id)", [shells.ST_ID_TYPE, sorted(set(sids))]).fetchall():
        n = by_code.get((str(org), str(sid).rpartition(":")[2])) or by_org.get(str(org))
        if n:
            out[sid] = n
    return out


def _tw_names(con, sids: list[str]) -> dict[str, str]:
    """security_id -> the official Chinese short name (公司簡稱, e.g. 台積電; else the full name without 股份有限公司) of
    the Taiwan lines among `sids`, from the MOPS basic-data payloads sync-mops stored (their aux_batch manifests);
    {} when none was fetched."""
    from .sources import mops
    tw = sorted({s for s in sids if str(s).split(":", 1)[0] in mops.TW_VENUES})
    if not tw:
        return {}
    code_of = {sid: str(code) for sid, code in con.execute(
        "SELECT security_id, id_value FROM identifiers WHERE id_type = ? AND list_contains(?::VARCHAR[], security_id)",
        [mops.ID_TYPE, tw]).fetchall()}
    want = set(code_of.values())
    raw: dict[str, str] = {}
    for (path,) in con.execute(
            "SELECT raw_path FROM snapshots WHERE source_id = ? AND kind = 'aux_batch' AND raw_path IS NOT NULL "
            "ORDER BY fetched_at DESC", [mops.BASIC_SOURCE_ID]).fetchall():
        if len(raw) == len(want):
            break
        try:
            entries = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for e in entries if isinstance(entries, list) else []:
            co = str((e or {}).get("co_id") or "")
            if co in want and co not in raw and str(e.get("kind") or "").startswith("basic-") and e.get("raw_path"):
                raw[co] = str(e["raw_path"])
    names: dict[str, str] = {}
    for co, rp in raw.items():
        try:
            short, full = mops.basic_names(json.loads(Path(rp).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        n = short or (re.sub(r"股份有限公司$|有限公司$", "", full) if full else None)
        if n:
            names[co] = n
    return {sid: names[code] for sid, code in code_of.items() if code in names}


LOOKUP_WAIT_S = 30.0      # how long a page build waits for a busy store before giving up (store.StoreLocked)


def _store_lookups(cfg, result: dict[str, Any], deck: dict[str, Any] | None = None
                   ) -> tuple[dict[str, str], dict[str, str], dict[str, Any], dict[str, str]]:
    """(company_key -> description text, security_id -> country, lineage, security_id -> local short name) from one
    short read-only session; empty when the store is missing or has an old schema (the page still renders).
    store.StoreLocked when the store stays busy (the caller decides: page_data)."""
    from . import screen, store
    listed = (list(result.get("rows") or [])[:MAX_ROWS] + list(result.get("unverified") or [])[:MAX_UNVERIFIED]
              + list(result.get("excluded_by_user") or []))
    keys = [r.get("company_key") for r in listed if r.get("company_key")]
    removed = list(result.get("excluded_by_scope") or []) + list(result.get("excluded_by_agent") or [])
    shown = [r.get("security_id") for r in listed + removed + list((deck or {}).get("cards") or [])[:MAX_CARDS]
             if r.get("security_id")]
    sids = [g.get("security_id") for part in ("no_description", "l2_profile_only")
            for g in (result.get("gaps") or {}).get(part) or [] if g.get("security_id")]
    descs: dict[str, str] = {}
    countries: dict[str, str] = {}
    local: dict[str, str] = {}
    lineage: dict[str, Any] = {"version": 1, "prev_run_id": None, "answers": 0}
    if not Path(cfg.db_path).exists():
        return descs, countries, lineage, local
    try:
        with store.session(cfg, read_only=True, wait_s=LOOKUP_WAIT_S) as con:
            if keys:
                by: dict[str, list[tuple[str, str, bool]]] = {}
                for ck, src, text in con.execute(
                        "SELECT company_key, source_id, text FROM descriptions WHERE text IS NOT NULL AND "
                        "list_contains(?::VARCHAR[], company_key) ORDER BY company_key, source_id",
                        [sorted(set(keys))]).fetchall():
                    by.setdefault(ck, []).append((src, text, True))
                for ck, ds in by.items():
                    d = screen.select_description(ds)
                    if d is not None:
                        descs[ck] = d["text"]
            if sids:
                countries = {sid: c for sid, c in con.execute(
                    "SELECT security_id, country FROM securities WHERE list_contains(?::VARCHAR[], security_id)",
                    [sorted(set(sids))]).fetchall() if c}
            with contextlib.suppress(Exception):         # an unreadable list: English names only
                local = _local_names(con, shown)
            with contextlib.suppress(Exception):         # Taiwan: the MOPS short names (公司簡稱)
                local.update({k: v for k, v in _tw_names(con, shown).items() if k not in local})
            run, n = result.get("run_id"), 1
            prev = (result.get("params") or {}).get("from_run")
            lineage["prev_run_id"] = prev
            if prev and result.get("supersedes"):
                lineage["prev_fill"] = _filled_by(con, prev)
            seen = {run}
            while prev and prev not in seen and n < 50:
                seen.add(prev)
                n += 1
                row = con.execute("SELECT params_json FROM screen_runs WHERE run_id = ?", [prev]).fetchone()
                try:
                    prev = (json.loads(row[0] or "{}") if row else {}).get("from_run")
                except ValueError:
                    prev = None
            lineage["version"] = n
    except store.StoreLocked:
        raise
    except Exception:  # noqa: BLE001 - old schema: the page renders without these extras
        pass
    lineage["answers"] = int(((result.get("calibration") or {}).get("answers")) or 0)
    return descs, countries, lineage, local


def with_translations(cfg, data: dict[str, Any], *, strict: bool = False) -> dict[str, Any]:
    """The page data with the stored agent translations of its foreign texts attached (jevscreen.translations);
    unchanged when the store has none. A store that stays busy: store.StoreLocked when `strict`, else the data is
    returned untranslated with data['store_busy'] = True (write_page then keeps or carries the earlier page's)."""
    from . import store, translations
    shas = translations.needed(data)
    if not shas or not Path(cfg.db_path).exists():
        return data
    try:
        with store.session(cfg, read_only=True, wait_s=LOOKUP_WAIT_S) as con:
            tr = translations.lookup(con, shas, data.get("lang") or "zh")
    except store.StoreLocked:
        if strict:
            raise
        data["store_busy"] = True
        return data
    except Exception:  # noqa: BLE001 - old schema: the originals show, marked not translated
        return data
    return translations.apply(data, tr)


def card_translations(cfg, deck: dict[str, Any] | None, lang: str, max_out: int | None = None
                      ) -> dict[int, dict[str, str | None]]:
    """card n -> {'what', 'quote'}: the stored agent translations into `lang` of a card's foreign texts (the same
    texts the page shows), for the `cards` printout. Empty when there are none or the store is busy."""
    cards, _chips = _cards(deck, max_out, lang)
    if not cards:
        return {}
    for c in cards:
        c["name_zh"] = c.get("name")         # the names are not printed from here
    data = with_translations(cfg, {"lang": "en" if lang == "en" else "zh", "cards": cards})
    return {int(c["n"]): {"what": c.get("what_tr"), "quote": (c.get("quote") or {}).get("text_tr")}
            for c in data.get("cards") or [] if c.get("what_tr") or (c.get("quote") or {}).get("text_tr")}


def read_page_data(path: str | Path) -> dict[str, Any] | None:
    """The data block of a written page (None when missing or unreadable)."""
    try:
        html = Path(path).read_text(encoding="utf-8")
        m = re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S)
        data = json.loads(m.group(1)) if m else None
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def stable_path(cfg, idea: str) -> Path:
    from . import keywords
    return Path(cfg.home) / "pages" / f"{keywords.idea_key(idea)}.html"


def _stable_owner_path(cfg, idea: str) -> Path:
    return stable_path(cfg, idea).with_suffix(".run.json")


def stable_run(cfg, idea: str) -> dict[str, Any] | None:
    """{'run_id', 'started_at'} of the run the stable page holds (None: unknown / no stable page)."""
    try:
        data = json.loads(_stable_owner_path(cfg, idea).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("run_id") else None


def stable_holds(cfg, idea: str, run_id: str | None) -> bool:
    """Does the stable page <home>/pages/<idea_key>.html hold this run?"""
    owner = stable_run(cfg, idea)
    return bool(run_id) and owner is not None and owner.get("run_id") == run_id and stable_path(cfg, idea).exists()


def _newest_for_stable(cfg, result: dict[str, Any]) -> bool:
    """The stable page always holds the newest run of the idea (spec 9.5): a page of an older run (`jevscreen page
    OLD`, `cards OLD`) only rewrites its own page.html. Runs compare by started_at (ISO text); a run without one
    counts as older than any recorded one."""
    owner = stable_run(cfg, result["idea"])
    if owner is None or owner.get("run_id") == result.get("run_id"):
        return True
    return str(result.get("started_at") or "") >= str(owner.get("started_at") or "")


def status_of(cfg, idea: str | None, lang: str, data: dict[str, Any] | None = None,
              job: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The one page's status block (prerequisites, progress) of an idea: from its quickstart job when there is one
    (`job`, else the job file), else from local files only (a plain screen). None when it cannot be built."""
    from . import pagestatus, quickstart
    try:
        if job is None and idea:
            job = quickstart.load_job(cfg, quickstart.idea_key(idea))
        return pagestatus.build(cfg, lang=lang, job=job,
                                result=result_facts(data) if data and data.get("run_id") else None)
    except Exception:  # noqa: BLE001 - the results still show without the status part
        return None


def page_data(cfg, result: dict[str, Any], deck: dict[str, Any] | None, *, lang: str = "zh",
              extra: dict[str, Any] | None = None, strict: bool = False,
              job: dict[str, Any] | None = None) -> dict[str, Any]:
    """The page data of a run with everything the store adds (descriptions, countries, lineage, official Chinese
    names, the idea's totals, the agent translations), without writing anything. A store that stays busy:
    store.StoreLocked when `strict` (the export must not work from a partial page), else the page without those
    extras and data['store_busy'] = True."""
    from . import store
    busy = False
    try:
        descs, countries, lineage, local = _store_lookups(cfg, result, deck)
    except store.StoreLocked:
        if strict:
            raise
        busy = True
        descs, countries, local = {}, {}, {}
        lineage = {"version": 1, "prev_run_id": None,
                   "answers": int(((result.get("calibration") or {}).get("answers")) or 0)}
    ex = dict(extra or {})
    ex.setdefault("has_sec_key", bool(cfg.sec_user_agent()))
    if "sec_declined" not in ex:
        from . import quickstart
        ex["sec_declined"] = quickstart.sec_declined(cfg)
    if "crawl_hidden" not in ex:
        from . import quickstart
        ex["crawl_hidden"] = quickstart.crawl_hidden(cfg, result.get("idea"))
    ex.setdefault("ondemand", ((result.get("layers") or {}).get("fetch")))   # the on-demand fetch, if one ran
    if "totals" not in ex and result.get("idea"):
        with contextlib.suppress(Exception):   # the idea's cumulative cost and time (quickstart.idea_totals)
            from . import quickstart
            ex["totals"] = quickstart.idea_totals(cfg, result["idea"], wait_s=5.0)
    if "unchecked" not in ex and result.get("output_dir"):
        with contextlib.suppress(Exception):     # companies your AI must still check (review.json)
            from . import review
            ex["unchecked"] = review.load_review(result["output_dir"]).get("unchecked") or []
    if "questions" not in ex and result.get("output_dir"):
        with contextlib.suppress(Exception):     # the scope-question slot of this version (review.json)
            from . import review_cli
            q = review_cli.page_questions(cfg, result, "en" if lang == "en" else "zh")
            if q is not None:
                ex["questions"] = q
    data = build_page_data(result, deck, lang=lang, lineage=lineage, descriptions=descs, country_of=countries,
                           extra=ex, local_names=local)
    if data.get("live") is None:
        data["live"] = status_of(cfg, result.get("idea"), data["lang"], data, job)
    if busy:
        data["store_busy"] = True
        return data
    return with_translations(cfg, data, strict=strict)


def write_page(cfg, out_dir: str | Path, result: dict[str, Any], deck: dict[str, Any] | None, *, lang: str = "zh",
               extra: dict[str, Any] | None = None, stable: bool = True,
               warn: Callable[[str], None] | None = None,
               job: dict[str, Any] | None = None) -> tuple[Path | None, dict[str, Any] | None]:
    """Write <out_dir>/page.html (and the stable copy <home>/pages/<idea_key>.html when this is the idea's newest
    run, see _newest_for_stable). Returns (path, data), or (None, None) after a warning when it could not be written
    (a configured secret in it, an I/O error). `job`: the idea's quickstart job as the caller holds it (default:
    its job file), for the page's status part."""
    def say(msg: str) -> None:
        (warn or (lambda m: print(m, file=sys.stderr)))(msg)
    try:
        from . import ops
        from . import translations
        out = Path(out_dir)
        path = out / "page.html"
        data = page_data(cfg, result, deck, lang=lang, extra=extra, job=job)
        busy = bool(data.pop("store_busy", False))
        if busy:
            # the store stayed busy: never replace a page with a poorer one (no descriptions, no translations).
            # This run's page, when written before, stays as it is; a new page carries the translations the
            # idea's page already showed.
            prev = read_page_data(path) if path.exists() else None
            if prev is not None:
                say("warning: result page not rebuilt: the database is busy (run the same command again later)")
                prev["store_busy"] = True
                return path, prev
            old = read_page_data(stable_path(cfg, result["idea"])) if result.get("idea") else None
            if old is not None and old.get("lang") == data.get("lang"):
                translations.apply(data, translations.carried(old))
        html = render_page(data)
        hits = ops.scan_secrets(cfg, html)
        if hits:
            say(f"warning: result page not written: it would contain a configured secret ({', '.join(hits)})")
            return None, None
        out.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(html, encoding="utf-8")
        os.replace(tmp, path)
        if stable and result.get("idea") and _newest_for_stable(cfg, result):
            from . import pagestatus
            sp = stable_path(cfg, result["idea"])
            sp.parent.mkdir(parents=True, exist_ok=True)
            with pagestatus.page_lock(cfg, result["idea"]):      # the status ticks rewrite the same file
                tmp = sp.with_suffix(f".{os.getpid()}.tmp")
                shutil.copyfile(path, tmp)
                os.replace(tmp, sp)
                owner = _stable_owner_path(cfg, result["idea"])
                owner.write_text(json.dumps({"run_id": result.get("run_id"),
                                             "started_at": result.get("started_at")}), encoding="utf-8")
        if busy:
            data["store_busy"] = True
        return path, data
    except Exception as e:  # noqa: BLE001 - a page failure is a warning, never an exit code
        say(f"warning: result page not written ({type(e).__name__}: {str(e)[:200]})")
        return None, None


def can_open_browser(env: dict[str, str] | None = None, platform: str | None = None,
                     browser_name: Callable[[], str | None] | None = None) -> bool:
    """macOS / Windows, or Linux with a display and a browser that is not a console one; never under CI, nor in the
    test suite (JEVSCREEN_TESTING=1: a path that forgot its fake opener never opens a real browser)."""
    env = os.environ if env is None else env
    platform = platform or sys.platform
    if env.get("CI") or env.get("JEVSCREEN_TESTING") == "1":
        return False
    if platform == "darwin" or platform.startswith("win"):
        return True
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        return False
    name = (browser_name or _browser_name)() or ""
    return not any(c in name.lower() for c in ("lynx", "w3m", "links", "elinks", "www-browser"))


def _browser_name() -> str | None:
    try:
        import webbrowser
        b = webbrowser.get()
        return getattr(b, "name", None) or type(b).__name__
    except Exception:  # noqa: BLE001
        return None


def open_page(path: str | Path, *, opener: Callable[[str], bool] | None = None, **kw: Any) -> bool:
    """Open the page in the browser when this machine can (can_open_browser); True only when open() said so."""
    if not can_open_browser(**kw):
        return False
    uri = Path(path).resolve().as_uri()
    try:
        if opener is None:
            import webbrowser
            opener = webbrowser.open
        return bool(opener(uri))
    except Exception:  # noqa: BLE001
        return False


def summary_of(data: dict[str, Any] | None, deck: dict[str, Any] | None) -> dict[str, int]:
    """The counts the chat says: listed = every row the page lists; with the sections (data['shortlist']) main =
    the confirmed list, to_confirm = the to-confirm section, and the evidence counts are the main list's."""
    rows = (data or {}).get("rows") or []
    main, confirm = main_and_confirm(data)
    out = {"listed": len(rows), "annual_report": sum(1 for r in main if r.get("evidence") == "annual_report"),
           "profile_only": sum(1 for r in main if r.get("evidence") == "profile"),
           "edge": sum(1 for r in rows if r.get("edge") or "edge" in (r.get("badges") or [])),
           "no_mention": sum(1 for r in rows if "no_mention" in (r.get("badges") or [])),
           "cards": len((deck or {}).get("cards") or [])}
    if (data or {}).get("shortlist"):
        out.update(main=len(main), to_confirm=len(confirm),
                   main_by_agent=sum(1 for r in main if r.get("main_via") == "agent"))
    return out


def to_confirm_brief(data: dict[str, Any] | None, n: int = 10) -> dict[str, Any] | None:
    """quickstart JSON 'to_confirm': the page's to-confirm section in brief ({n, rows: the first n as name / ticker /
    security_id (for keep= / drop=) / country / verdict words / evidence kind / your AI's call / moved out of the main
    list by your AI / not yet checked by your AI}); None for a single padded list."""
    if not (data or {}).get("shortlist"):
        return None
    lang = (data or {}).get("lang") or "zh"
    _main, confirm = main_and_confirm(data)
    return {"n": len(confirm), "rows": [
        {"name": display_name(r, lang), "name_en": r.get("name"), "ticker": r.get("ticker"),
         "country": r.get("country"), "verdict_words_zh": STRINGS["zh"].get(verdict_key(r, True)),
         "verdict_words_en": STRINGS["en"].get(verdict_key(r, True)),
         "evidence_kind": r.get("evidence"), "agent": (r.get("agent") or {}).get("v"),
         "moved_by_agent": r.get("main_via") == "agent_no", "unchecked": bool(r.get("unchecked")),
         "security_id": r.get("security_id")} for r in confirm[:n]]}


def top_more(data: dict[str, Any] | None, shown: int) -> int:
    """quickstart JSON 'top_more': the confirmed rows beyond the `shown` first ones of 'top' (the chat relays at
    most 10; the page lists every confirmed row). 0 without the sections (a single padded list) or when all fit."""
    if not (data or {}).get("shortlist"):
        return 0
    return max(0, len(main_and_confirm(data)[0]) - int(shown))


def top_rows(data: dict[str, Any] | None, n: int = 10, *, section: str = "main") -> list[dict[str, Any]]:
    """The first n rows of the main list (section 'main'; with the sections that is the confirmed list only, never
    padded with the to-confirm section) or of the whole list (section 'all') for the agent to relay in chat
    (quickstart JSON 'top'), in the page's language: 'name' is the
    name the page shows (on a Chinese page the official Chinese short name, else the agent's translation, else the
    English name), 'name_en' the English one; 'one_line' what the page shows (the agent's translation when there is
    one: 'one_line_translated' true, the verbatim text in 'one_line_original'; 'one_line_needs_translation' true when
    it is still in another language); 'excerpt_mentions_idea' false = none of the text the AI read has the idea's
    words (say so, it is a gap; null: nothing to check); 'edge' borderline; 'user' the user answered this company;
    'verdict_from_user' it is listed only because of that answer (the AI did not confirm it from the text)."""
    lang = (data or {}).get("lang") or "zh"
    out = []
    rows = main_and_confirm(data)[0] if section == "main" else ((data or {}).get("rows") or [])
    split = bool((data or {}).get("shortlist"))
    for r in rows[:n]:
        v = r.get("verdict") or "partial"
        q = r.get("quote") or {}
        out.append({"rank": r.get("rank"), "verdict": v, "name": display_name(r, lang), "name_en": r.get("name"),
                    "name_zh": r.get("name_zh"), "name_translated": bool(lang == "zh" and not r.get("name_zh")
                                                                       and r.get("name_tr")),
                    "ticker": r.get("ticker"), "country": r.get("country"),
                    "country_zh": r.get("country_zh"),
                    "verdict_words_zh": STRINGS["zh"].get(verdict_key(r, split), v),
                    "verdict_words_en": STRINGS["en"].get(verdict_key(r, split), v),
                    "evidence_kind": r.get("evidence"), "one_line": shown_text(r, "one_line"),
                    "one_line_original": r.get("one_line") if r.get("one_line_tr") else None,
                    "one_line_source": r.get("one_line_source"),
                    "one_line_translated": bool(r.get("one_line_tr")),
                    "one_line_needs_translation": bool(r.get("one_line_x") and not r.get("one_line_tr")),
                    "excerpt_mentions_idea": q.get("mentions"), "edge": bool(r.get("edge")),
                    "user": bool(r.get("user")), "verdict_from_user": bool(r.get("user_only")),
                    "agent": (r.get("agent") or {}).get("v"), "agent_state": (r.get("agent") or {}).get("state"),
                    f"agent_why_{lang}": (r.get("agent") or {}).get("why"),
                    "scope_demoted": bool(r.get("scope_demoted")),
                    # in the main list because your AI confirmed it from the text (say so: 你的 AI 核对)
                    "checked_by_agent": r.get("main_via") == "agent",
                    # entered after a fill / re-rank and your AI has not checked it yet (say so: 未核对)
                    "unchecked": bool(r.get("unchecked")),
                    # the decide tokens that override your AI / the evidence for this row (the human's call)
                    "overrides": {"keep": f"keep={r.get('security_id')}", "drop": f"drop={r.get('security_id')}"}
                    if r.get("security_id") else None})
    return out


def iter_strings_keys() -> Iterable[tuple[str, set[str]]]:
    return ((lang, set(t)) for lang, t in STRINGS.items())
