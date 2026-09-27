"""The result page: one self-contained page.html per screen run (and a stable copy per idea) that a non-technical
human opens in a browser.

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
- Cards: the answer tokens come from calib.answer_tokens (Python); the page only joins them, so the line it builds
  ("jevscreen answer \"1a 2no 3c 4?\" --deck deck-<run>-<n>") is exactly what `jevscreen answer` parses.
- Before writing, the page is scanned for every configured secret (ops.scan_secrets); a hit means no page.
- A page failure is a warning, never an exit code.
"""
from __future__ import annotations

import contextlib
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
QUOTE_CHARS = 300
CNY_PER_USD = 7.2            # fixed, labelled rate for the ≈ ¥ figure next to USD
CRAWL_REQ_PER_S = 1.96       # measured effective profile-crawl rate (minutes estimate of the China gap fill)
BADGE_ORDER = ("no_mention", "profile_only", "stale", "edge", "read_once", "backfill")
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
        "rank_note": "排序＝与你的想法吻合的程度（证据越直接越靠前，同等时按市值）。不看估值和财务，不是买入建议。",
        "banner": "仅供你个人研究：本页含只限个人使用的 TradingView / Yahoo 数据和年报原文摘录。请勿转发、截图公开或上传。",
        "legend_title": "怎么读这一页",
        "legend_fact": "事实＝年报原文，附链接", "legend_inference": "推断＝AI 的判断",
        "legend_gap": "缺口＝没拿到或没读到的", "legend_user": "你的判断＝你在卡片上答的",
        "tag_fact": "事实", "tag_inference": "推断", "tag_gap": "缺口", "tag_user": "你的判断",
        "search": "按名字或代码查找…", "search_none": "这页没有它？让你的 AI 运行 jevscreen why <名字>，会告诉你原因",
        "filter_all": "全部", "filter_report": "有年报", "filter_edge": "边缘",
        "list_title": "入选名单（{n} 家）", "list_empty": "这次没有公司通过核对。",
        "verdict_explicit": "明确符合", "verdict_partial": "相关",
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
        "top_title": "前 {n} 名一览", "col_rank": "#", "col_name": "公司", "col_verdict": "结论", "col_evidence": "证据",
        "ev_short_annual_report": "年报", "ev_short_profile": "简介", "ev_short_gap": "年报没提到",
        "ev_short_gap_profile": "简介没提到",
        "unverified_group": "{n} 家：{why}",
        "mcap": "市值", "badges": "标记", "what": "做什么",
        "badge_profile_only": "只有简介，没有年报（证据弱）", "badge_stale": "年报超过 3 年",
        "badge_edge": "边缘：多读几次可能变", "badge_read_once": "只读了一次", "badge_backfill": "递补，未经核对",
        "badge_no_mention": "边缘：摘录没提到你的想法",
        "badge_user": "你的判断",
        "cards_title": "帮系统判断几家（可选，约 3–5 分钟）",
        "cards_intro": "每张点一个按钮，不想答就跳过。答完把下面那行复制给你的 AI；应用后会重新排序（约 $0.01–0.03，不再下载）。",
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
        "gaps_title": "缺口（没读到的，不是“不相关”）",
        "gap_no_description": "{n} 家没有任何简介，这次没读（不是“不相关”）：{by}。补中国：让你的 AI 运行 {cmd}（约 {m} 分钟，可后台）。",
        "gap_no_description_other": "{n} 家没有任何简介，这次没读（不是“不相关”）：{by}。",
        "gap_us_profile": "美股 {k} 家只用简介核对。想用年报原文核对，可以在下载时按 SEC 规则附上一个名字和邮箱（可选，"
                          "不用注册账号，只发给 sec.gov）：{cmd}。",
        "gap_jp_profile": "日本 {k} 家只用简介核对（这台电脑没有日本年报数据包）。",
        "gap_ondemand": "这次临时补了 {n} 家的年报原文（{src}）。",
        "footer": "运行 {run} · 卡组 {deck} · jev-screen {ver} · 仅供个人研究",
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
        "rank_note": "Rank = how well the company fits your idea (more direct evidence first, then market cap). It "
                     "ignores valuation and financials and is not a recommendation to buy.",
        "banner": "For your personal research only: this page contains personal-use-only TradingView / Yahoo data "
                  "and verbatim annual-report excerpts. Do not forward, post screenshots or upload it.",
        "legend_title": "How to read this page",
        "legend_fact": "Fact = verbatim filing text, with link", "legend_inference": "Inference = the AI's judgment",
        "legend_gap": "Gap = what we could not get or read", "legend_user": "Your call = what you answered on a card",
        "tag_fact": "Fact", "tag_inference": "Inference", "tag_gap": "Gap", "tag_user": "Your call",
        "search": "Find by name or ticker…",
        "search_none": "Not on this page? Ask your AI to run jevscreen why <name>; it will tell you why",
        "filter_all": "All", "filter_report": "With annual report", "filter_edge": "Borderline",
        "list_title": "The list ({n} companies)", "list_empty": "No company passed the check this time.",
        "verdict_explicit": "Clearly fits", "verdict_partial": "Related",
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
        "top_title": "Top {n} at a glance", "col_rank": "#", "col_name": "Company", "col_verdict": "Verdict",
        "col_evidence": "Evidence",
        "ev_short_annual_report": "report", "ev_short_profile": "profile",
        "ev_short_gap": "not in the report", "ev_short_gap_profile": "not in the profile",
        "unverified_group": "{n}: {why}",
        "mcap": "Market cap", "badges": "Marks", "what": "What it does",
        "badge_profile_only": "Profile only (weaker)", "badge_stale": "Filing older than 3 years",
        "badge_edge": "Borderline: may change on re-reading", "badge_read_once": "Read once",
        "badge_backfill": "Filled in, not checked", "badge_user": "Your call",
        "badge_no_mention": "Borderline: the excerpt does not mention your idea",
        "cards_title": "Help sharpen the list (optional, about 3–5 min)",
        "cards_intro": "Tap one button per card; skip any. When done, copy the line below to your AI; applying "
                       "re-ranks the list (about $0.01–0.03, no downloads).",
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
        "gaps_title": "Gaps (not read, which is not the same as not relevant)",
        "gap_no_description": "{n} {companies_have} no profile at all and were not read (not \"not relevant\"): {by}. "
                              "To fill China: ask your AI to run {cmd} (about {m} min, can run in the background).",
        "gap_no_description_other": "{n} {companies_have} no profile at all and were not read (not \"not "
                                    "relevant\"): {by}.",
        "gap_us_profile": "{k} US {companies_were} checked from profiles only. To check {them} against annual reports, "
                          "you can give the SEC a contact name and e-mail, sent with each download (optional, no account): {cmd}.",
        "gap_jp_profile": "{k} Japanese {companies_were} checked from profiles only (no Japanese filing pack on this "
                          "computer).",
        "gap_ondemand": "Annual-report text was fetched on demand for {n} companies ({src}).",
        "footer": "run {run} · deck {deck} · jev-screen {ver} · personal research only",
        "dur_ms": "{m} min {s} s", "dur_s": "{s} s",
        "no_js": "This page needs JavaScript for the full view. The plain-text list follows:",
    },
}
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
    """company_key -> {'sha': evidence_sha, 'pieces': [text, ...]} from <run_dir>/l2_inputs.jsonl: every piece of
    text layer 2 read for the company (the excerpts: overview and context pieces; without excerpts the input text
    without its header line). {} when the file is missing or unreadable."""
    if not run_dir:
        return {}
    want = set(keys) if keys is not None else None
    out: dict[str, dict[str, Any]] = {}
    try:
        with open(Path(run_dir) / "l2_inputs.jsonl", encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                ck = d.get("company_key") if isinstance(d, dict) else None
                if not ck or (want is not None and ck not in want):
                    continue
                ex = d.get("excerpts")
                pieces = [str(e.get("text")) for e in ex if isinstance(e, dict) and e.get("text")] \
                    if isinstance(ex, list) else []
                if not pieces and d.get("text"):
                    body = str(d["text"])
                    if body.startswith("["):
                        body = body.split("\n", 1)[1] if "\n" in body else ""
                    pieces = [body.strip()] if body.strip() else []
                out[ck] = {"sha": d.get("evidence_sha"), "pieces": pieces}
    except OSError:
        return {}
    return out


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
            "verdict": r.get("l2_label") if r.get("l2_label") in ("explicit", "partial") else (uv or None),
            "evidence": r.get("l2_evidence") or "profile", "quote": q, "badges": badges,
            "edge": "edge" in badges or no_mention,
            "user": user,
            # listed only because of the user's answer: the AI did not confirm it from the text
            "user_only": user and r.get("l2_label") not in ("explicit", "partial"),
            "details": {"reads": _reads(r), "l1": r.get("l1_label"), "l2": r.get("l2_label"),
                        "p_pos": r.get("l2_p_pos"), "p_core": None if r.get("l1_p_core") is None
                        else round(float(r["l1_p_core"]), 2)}}


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
        if cn:
            out.append({"id": "no_description", "count": len(nod), "cn": cn, "minutes": m, "command": cmd,
                        "text_zh": STRINGS["zh"]["gap_no_description"].format(n=len(nod), by=by_zh, cmd=cmd, m=m),
                        "text_en": STRINGS["en"]["gap_no_description"].format(n=len(nod), by=by_en, cmd=cmd, m=m,
                                                                                 **_en_count(len(nod)))})
        else:
            out.append({"id": "no_description", "count": len(nod), "cn": 0, "minutes": None, "command": None,
                        "text_zh": STRINGS["zh"]["gap_no_description_other"].format(n=len(nod), by=by_zh),
                        "text_en": STRINGS["en"]["gap_no_description_other"].format(n=len(nod), by=by_en,
                                                                                       **_en_count(len(nod)))})
    prof = gaps.get("l2_profile_only") or []
    us = [g for g in prof if country(g.get("security_id")) == "United States"
          or str(g.get("security_id") or "").split(":", 1)[0] in ("NASDAQ", "NYSE", "AMEX", "NYSEARCA")]
    if us and not extra.get("has_sec_key"):
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
    rows_in = list(result.get("rows") or [])[:MAX_ROWS]
    if l2_pieces is None:
        l2_pieces = load_l2_pieces(result.get("output_dir"), [r.get("company_key") for r in rows_in])
    rows = [_row(r, _first_sentence(descriptions.get(r.get("company_key") or "")), today, terms,
                 local_names.get(r.get("security_id") or ""), l2_pieces.get(r.get("company_key") or ""), lang)
            for r in rows_in]
    unv = [{"name": _clean_name(r.get("name")) or r.get("security_id"), "ticker": _ticker(r.get("security_id")),
            "name_zh": local_names.get(r.get("security_id") or ""),
            "country": r.get("country"), "country_zh": COUNTRY_ZH.get(r.get("country") or ""),
            "country_text": l10n.country_words(r.get("country"), lang), "status": r.get("l2_status") or "not_run"}
           for r in list(result.get("unverified") or [])[:MAX_UNVERIFIED]]
    exc = [{"name": _clean_name(r.get("name")) or r.get("security_id"), "ticker": _ticker(r.get("security_id")),
            "name_zh": local_names.get(r.get("security_id") or ""), "note": r.get("user_note")}
           for r in result.get("excluded_by_user") or []]
    cards, chip_text = _cards(deck, params.get("max_out"), lang)
    for c in cards:
        c["name_zh"] = local_names.get(c.get("security_id") or "")
    if lang == "en":                  # an English page shows English names only
        for x in rows + unv + exc + cards:
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
                   "floor_text": _floor_text(params.get("min_mcap_usd"), lang)},
        "rows": rows, "unverified": unv, "unverified_total": len(result.get("unverified") or []),
        "unverified_groups": _unverified_groups(result.get("unverified") or []),
        "excluded": exc, "cards": cards, "chips": chip_text, "idea_terms": idea_words_shown(result, lang), "top_n": TOP_TABLE,
        "answer_prefix": "jevscreen answer", "gaps": _gap_lines(result, country_of or {}, extra),
        "strings": {lang: STRINGS[lang]},
    }
    return tr_mod.apply(data, translations)


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
    if data.get("version", 1) > 1:
        change = data.get("change") if isinstance(data.get("change"), dict) else None
        if change and change.get(lang):                  # what changed (the page header says the same)
            out.append(S["version_change"].format(n=data["version"], change=change[lang],
                                                  prev=data.get("prev_run_id") or "?"))
        else:
            key = "version_n" if data.get("answers") else "version_n0"
            out.append(S[key].format(n=data["version"], k=data.get("answers"), prev=data.get("prev_run_id") or "?"))
    f = data.get("funnel") or {}
    out.append(S["funnel"].format(universe=f.get("universe"), floor=_floor_text(f.get("floor"), lang),
                                  described=f.get("described"), l1=f.get("l1"), listed=f.get("listed")))
    out.append(S["rank_note"])
    out.append("")
    rows = data.get("rows") or []
    out.append(S["list_title"].format(n=len(rows)))
    if not rows:
        out.append(S["list_empty"])
    for r in rows:
        name = display_name(r, lang)
        # a translated name keeps the original name beside it (small on the page)
        other = r.get("name") if zh and not r.get("name_zh") and r.get("name_tr") else None
        country = l10n.country_words(r.get("country"), lang)
        where = " · ".join(x for x in (r.get("ticker"), country) if x)
        verdict = S.get(f"verdict_{r.get('verdict') or 'partial'}", r.get("verdict") or "")
        q = r.get("quote") or {}
        if r.get("user_only"):
            ev = S["evidence_user"]
        elif q.get("mentions") is False:
            ev = S["quote_gap_profile" if r.get("evidence") == "profile" else "quote_gap"]
        else:
            ev = S.get(f"evidence_{r.get('evidence') or 'profile'}", "")
        marks = ("；" if zh else "; ").join([S.get(f"badge_{b}", b) for b in r.get("badges") or []]
                                           + ([S["badge_user"]] if r.get("user") else []))
        line = f"#{r.get('rank')} {name}" + (f"（{other}）" if other else "")
        out.append(f"{line} {where} — {verdict} · {ev}" + (f" [{marks}]" if marks else ""))
        if r.get("one_line"):
            one = shown_text(r, "one_line")
            tag = S["text_ai"] if r.get("one_line_tr") else (S["text_untranslated"] if r.get("one_line_x") else "")
            out.append(f"    {S['what']}{sep}{one}{tag}")
    groups = data.get("unverified_groups") or []
    if groups:
        out.append("")
        out.append(S["unverified_title"].format(n=data.get("unverified_total")))
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
--infer:#7a4b12;--gap:#8a2d2d;--user:#4a3a8a;--badge:#f3ede0;--bar:#ffffffee;--on:#ffffff}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--fg:#ecebe6;--muted:#a3a198;--line:#34332f;--card:#1f1f1c;
--accent:#7fb6de;--fact:#8fcf9a;--infer:#e2b36d;--gap:#ef9a9a;--user:#b9aef2;--badge:#2b2a26;--bar:#1f1f1cee;
--on:#111110}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",
"PingFang SC","Hiragino Sans GB","Microsoft YaHei","Noto Sans CJK SC","Noto Sans SC",sans-serif}
body.hasbar{padding-bottom:96px}
main{max-width:980px;margin:0 auto;padding:16px}
h1{font-size:1.35rem;margin:.2rem 0}h2{font-size:1.1rem;margin:1.6rem 0 .6rem}
.muted{color:var(--muted)}.small{font-size:.88rem}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap}
button{font:inherit;min-height:44px;min-width:44px;padding:6px 14px;border-radius:10px;border:1px solid var(--line);
background:var(--card);color:var(--fg);cursor:pointer}
button.on{background:var(--accent);color:var(--on);border-color:var(--accent)}
button:disabled{opacity:.5;cursor:default}
.banner{border:1px solid var(--gap);color:var(--gap);border-radius:10px;padding:10px 12px;margin:12px 0}
.legend{display:flex;flex-wrap:wrap;gap:8px 16px;font-size:.9rem;margin:8px 0}
.tag{display:inline-block;font-size:.75rem;border-radius:6px;padding:0 6px;margin-right:6px;border:1px solid}
.t-fact{color:var(--fact);border-color:var(--fact)}.t-inference{color:var(--infer);border-color:var(--infer)}
.t-gap{color:var(--gap);border-color:var(--gap)}.t-user{color:var(--user);border-color:var(--user)}
.t-ai{color:var(--accent);border-color:var(--accent)}.t-orig{color:var(--muted);border-color:var(--muted)}
.orig{margin:4px 0;padding:4px 8px;border-left:3px solid var(--line);color:var(--muted)}
input[type=search]{width:100%;min-height:44px;font:inherit;padding:8px 12px;border-radius:10px;
border:1px solid var(--line);background:var(--card);color:var(--fg)}
.filters{display:flex;gap:8px;margin:10px 0;flex-wrap:wrap}
.row,.cardq{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:10px 0}
.head{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.rank{font-weight:700;color:var(--accent)}.name{font-weight:600}
.badge{background:var(--badge);border-radius:6px;padding:1px 8px;font-size:.82rem}
blockquote{margin:8px 0;padding:6px 10px;border-left:3px solid var(--fact);background:transparent}
details{margin-top:6px}summary{cursor:pointer;color:var(--muted)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:4px 16px;font-size:.9rem}
.btns{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
.bar{position:fixed;left:0;right:0;bottom:0;background:var(--bar);border-top:1px solid var(--line);padding:8px 16px}
.bar .in{max-width:980px;margin:0 auto;display:flex;gap:8px;align-items:center}
.bar textarea{flex:1 1 auto;min-width:0;height:44px;font:13px/1.35 ui-monospace,Menlo,Consolas,monospace;padding:5px 8px;
border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg);resize:none}
.bar .msg{position:absolute;left:16px;bottom:62px}
a{color:var(--accent)}
blockquote.gapq{border-left-color:var(--gap)}
.clamp{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
button.more{min-height:32px;padding:2px 0;border:0;background:transparent;color:var(--accent);font-size:.88rem}
.tablewrap{max-width:100%;overflow-x:auto}
table.top10{width:100%;border-collapse:collapse;font-size:.92rem;background:var(--card);border:1px solid var(--line);
border-radius:12px}
table.top10 th,table.top10 td{text-align:left;vertical-align:top;padding:6px 8px;border-bottom:1px solid var(--line);
overflow-wrap:anywhere}
table.top10 th{color:var(--muted);font-weight:600;font-size:.82rem;overflow-wrap:normal}
table.top10 td:first-child,table.top10 th:first-child{white-space:nowrap;width:1%;overflow-wrap:normal}
table.top10 td:nth-child(3),table.top10 td:nth-child(4){word-break:keep-all;overflow-wrap:normal}
table.top10 td.t-gap{color:var(--gap)}table.top10 td.t-user{color:var(--user)}
table.top10 tr.edge td:first-child a{color:var(--gap)}
details.unv .names{margin:0 0 10px 0}
main,blockquote,.row,.cardq{overflow-wrap:anywhere}
pre.plain{white-space:pre-wrap;overflow-wrap:anywhere;font-size:14px;line-height:1.5;font-family:inherit;padding:0 16px}
footer{margin:28px 0 8px;font-size:.82rem;color:var(--muted)}
@media (max-width:720px){.grid{grid-template-columns:1fr}main{padding:12px 16px}.bar .lbl{display:none}
.bar{padding:6px 12px}.bar textarea{height:40px}body.hasbar{padding-bottom:80px}}
@media print{.bar,.filters,input[type=search],button{display:none}body{padding:0}}
"""

JS = r"""
(function(){
var D=JSON.parse(document.getElementById('data').textContent);
var L=D.lang==='en'?'en':'zh', Q='', F='all', A={}, DBG=false;
try{DBG=/[?&]debug\b/.test(String(window.location.search||''));}catch(e){}
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
function origToggle(orig){var w=E('div','small');var o=E('div','orig');o.style.display='none';o.appendChild(tag('fact',S('orig_label')));o.appendChild(T(orig));
 var bt=E('button','more',S('show_orig'));bt.onclick=function(){var shut=o.style.display==='none';o.style.display=shut?'block':'none';bt.textContent=S(shut?'hide_orig':'show_orig');};
 w.appendChild(bt);w.appendChild(o);return w;}
function trLine(cls,prefix,orig,tr,foreign){var d=E('div',cls);if(prefix)d.appendChild(T(prefix));
 if(foreign&&tr){d.appendChild(T(tr+' '));d.appendChild(tag('ai',S('ai_tr')));d.appendChild(origToggle(orig));}
 else{if(foreign){d.appendChild(tag('orig',S('untranslated')));}d.appendChild(T(orig));}return d;}
function clampBox(b,tx){if(String(tx.textContent).length<=60)return;tx.className='clamp';var mb=E('button','more',S('more'));
 mb.onclick=function(){var shut=tx.className==='clamp';tx.className=shut?'':'clamp';mb.textContent=S(shut?'less':'more');};b.appendChild(mb);
 setTimeout(function(){try{if(tx.className==='clamp'&&tx.scrollHeight>0&&tx.scrollHeight<=tx.clientHeight+2)mb.style.display='none';}catch(e){}},0);}
function quote(q,profile,byUser){var b=E('div');
 if(!q||!q.text){b.appendChild(tag('fact'));b.appendChild(E('span','muted',S('no_quote')));return b;}
 var miss=gapq(q),pr=profile||q.profile||q.source==='profile',trd=!!(q.text_x&&q.text_tr);
 if(miss)b.appendChild(tag('gap',S(pr?'quote_gap_profile':'quote_gap')));else if(!trd)b.appendChild(tag('fact'));
 if(trd)b.appendChild(tag('ai',S('ai_tr_quote')));else if(q.text_x)b.appendChild(tag('orig',S('untranslated')));
 var bq=E('blockquote',miss?'gapq':null);var tx=E('div',null,trd?q.text_tr:q.text);bq.appendChild(tx);b.appendChild(bq);clampBox(b,tx);
 if(trd)b.appendChild(origToggle(q.text));
 if(miss)b.appendChild(E('div','small muted',fmt(S(byUser?'quote_gap_why_user':'quote_gap_why'),{terms:(D.idea_terms||[]).slice(0,8).join(L==='zh'?'、':', ')})));
 var src=[q.where||(pr?S('evidence_profile'):null),q.date_text].filter(Boolean).join(' · ');
 if(src||isHttp(q.url)){var m=E('div','small muted',src?S('quote_from')+(L==='zh'?'：':': ')+src+' ':'');if(isHttp(q.url))m.appendChild(link(q.url));b.appendChild(m);}return b;}
function answerLine(){var parts=[];(D.cards||[]).forEach(function(c){var a=A[c.n];if(!a||!a.v)return;
 var t=c.tokens||{};var k=a.chip||(a.v==='?'?'?':a.v);if(t[k])parts.push(t[k]);});
 if(!parts.length)return '';return D.answer_prefix+' "'+parts.join(' ')+'" --deck '+D.deck_id+(L==='en'?' --lang en':'');}
function evCell(r){if(r.user_only)return [S('tag_user'),'t-user'];
 if(gapq(r.quote)&&!r.user)return [S(r.evidence==='profile'?'ev_short_gap_profile':'ev_short_gap'),'t-gap'];
 return [S('ev_short_'+(r.evidence||'profile'))+(r.user?' · '+S('tag_user'):''),null];}
function topTable(m){var rows=(D.rows||[]).slice(0,D.top_n||10);if(!rows.length)return;
 m.appendChild(E('h2',null,fmt(S('top_title'),{n:rows.length})));var t=E('table','top10');var th=E('tr');
 ['col_rank','col_name','col_verdict','col_evidence'].forEach(function(k){th.appendChild(E('th',null,S(k)));});t.appendChild(th);
 rows.forEach(function(r){var tr=E('tr',r.edge?'edge':null);var a=E('a',null,'#'+r.rank);a.href='#r'+r.rank;var c0=E('td');c0.appendChild(a);tr.appendChild(c0);
  var c1=E('td');c1.appendChild(E('div','name',nm(r)));if(sub(r))c1.appendChild(E('div','small muted',sub(r)));c1.appendChild(E('div','small muted',[r.ticker,ctry(r)].filter(Boolean).join(' · ')));tr.appendChild(c1);
  tr.appendChild(E('td',null,S('verdict_'+(r.verdict||'partial'))+(r.edge?' · '+S('filter_edge'):'')));
  var ev=evCell(r);tr.appendChild(E('td',ev[1],ev[0]));t.appendChild(tr);});
 var w=E('div','tablewrap');w.appendChild(t);m.appendChild(w);}
function render(){
 document.documentElement.lang=L==='zh'?'zh-CN':'en';document.title=S('title')+' · '+(D.headline||D.idea||'');
 var app=document.getElementById('app');app.textContent='';var m=E('main');app.appendChild(m);
 var top=E('div','top');var h=E('div');h.appendChild(E('div','muted small',S('idea')));h.appendChild(E('h1',null,D.headline||D.idea));
 var alt=D.headline_is_idea_en?[S('idea_orig'),D.idea]:((D.idea_en&&D.idea_en!==(D.headline||D.idea))?[S('idea_en'),D.idea_en]:null);
 if(alt){var ad=E('details','small muted');ad.appendChild(E('summary',null,alt[0]));ad.appendChild(E('div','orig',alt[1]));h.appendChild(ad);}
 top.appendChild(h);m.appendChild(top);
 m.appendChild(E('div','small',D.version>1?(D.change&&D.change[L]?fmt(S('version_change'),{n:D.version,change:D.change[L],prev:D.prev_run_id||'?'}):fmt(S(D.answers?'version_n':'version_n0'),{n:D.version,k:D.answers,prev:D.prev_run_id||'?'})):S('version_first')));
 var st=S('status_'+D.status);if(st==='status_'+D.status)st=S('status_other');
 m.appendChild(E('div','small muted',fmt(S(D.totals?'meta_total':'meta'),{date:D.date_text||D.date,status:st,cost:money(D.cost_usd),
  cny:(L==='zh'&&D.cost_cny!==null&&D.cost_cny!==undefined)?fmt(S(D.cost_usd>0&&D.cost_cny<0.005?'cny_tiny':'cny'),{y:D.cost_cny.toFixed(2)}):'',time:dur(D.seconds)}).replace(/^ · /,'')));
 var f=D.funnel||{};m.appendChild(E('p',null,fmt(S('funnel'),{universe:f.universe,floor:f.floor_text||'?',described:f.described,l1:f.l1,listed:f.listed})));
 topTable(m);
 m.appendChild(E('p','small',S('rank_note')));
 m.appendChild(E('div','banner',S('banner')));
 var lg=E('div','legend');['fact','inference','gap','user'].forEach(function(k){var s=E('span');s.appendChild(tag(k));s.appendChild(T(S('legend_'+k)));lg.appendChild(s);});
 if(D.translation&&D.translation.foreign){var sa=E('span');sa.appendChild(tag('ai',S('ai_tr')));sa.appendChild(T(S('legend_ai')));lg.appendChild(sa);}
 m.appendChild(lg);
 var sb=E('input');sb.type='search';sb.placeholder=S('search');sb.value=Q;sb.setAttribute('aria-label',S('search'));m.appendChild(sb);
 var fl=E('div','filters');['all','report','edge'].forEach(function(k){var b=E('button',F===k?'on':null,S('filter_'+k));b.onclick=function(){F=k;render();};fl.appendChild(b);});m.appendChild(fl);
 m.appendChild(E('h2',null,fmt(S('list_title'),{n:(D.rows||[]).length})));
 var list=E('div');m.appendChild(list);var none=E('p','muted',S('search_none'));none.style.display='none';m.appendChild(none);
 function draw(){list.textContent='';var q=Q.trim().toLowerCase(),shown=0;
  (D.rows||[]).forEach(function(r){
   if(F==='report'&&r.evidence!=='annual_report')return;if(F==='edge'&&!r.edge)return;
   if(q&&[r.name,r.name_zh,r.name_tr,r.ticker].every(function(x){return String(x||'').toLowerCase().indexOf(q)<0;}))return;
   shown++;var d=E('div','row');d.id='r'+r.rank;var hd=E('div','head');hd.appendChild(E('span','rank','#'+r.rank));hd.appendChild(E('span','name',nm(r)));
   hd.appendChild(E('span','muted small',[r.ticker,ctry(r)].filter(Boolean).join(' · ')));
   if((r.badges||[]).length)hd.appendChild(E('span','badge',S('badge_'+r.badges[0])));d.appendChild(hd);
   if(sub(r)){var sn=E('div','small muted',sub(r));d.appendChild(sn);}
   if(r.one_line)d.appendChild(trLine('small',null,r.one_line,r.one_line_tr,r.one_line_x));
   var v=E('div');v.appendChild(tag('inference'));v.appendChild(E('strong',null,S('verdict_'+(r.verdict||'partial'))));
   v.appendChild(T(' · '+S(r.user_only?'evidence_user':'evidence_'+(r.evidence||'profile'))));
   if(r.user){v.appendChild(T(' '));v.appendChild(tag('user'));}d.appendChild(v);
   d.appendChild(quote(r.quote,r.evidence==='profile',r.user));
   var det=E('details');det.appendChild(E('summary',null,S('details')));var g=E('div','grid');
   function kv(k,val){if(val===null||val===undefined||val==='')return;g.appendChild(E('div','muted',S(k)));g.appendChild(E('div',null,val));}
   var rd=r.details||{};kv('reads_label',rd.reads?(rd.reads.n>1?fmt(S('reads'),rd.reads):S('reads_one')):null);
   kv('l1',lbl(rd.l1));kv('l2',lbl(rd.l2));kv('p_pos',pct(rd.p_pos));if(DBG)kv('p_core',rd.p_core);kv('mcap',r.mcap_text);
   kv('badges',(r.badges||[]).map(function(b){return S('badge_'+b);}).join(L==='zh'?'；':'; '));det.appendChild(g);d.appendChild(det);
   list.appendChild(d);});
  if(!(D.rows||[]).length&&!q)list.appendChild(E('p','muted',S('list_empty')));
  none.style.display=(q&&!shown)?'block':'none';}
 sb.oninput=function(){Q=sb.value;draw();};draw();
 m.appendChild(E('h2',null,S('cards_title')));
 if(!(D.cards||[]).length){m.appendChild(E('p','muted',S('no_cards')));}else{m.appendChild(E('p','small',S('cards_intro')));}
 var colon=L==='zh'?'：':': ';
 (D.cards||[]).forEach(function(c){var a=A[c.n]||{};var d=E('div','cardq');var hd=E('div','head');
  hd.appendChild(E('span','rank','['+c.n+']'));hd.appendChild(E('span','name',nm(c)));
  hd.appendChild(E('span','muted small',[c.ticker,c.rank?fmt(S('card_rank'),{rank:c.rank}):S('card_not_listed')].filter(Boolean).join(' · ')));
  if(c.edge)hd.appendChild(E('span','badge',S('badge_edge')));d.appendChild(hd);
  if(sub(c))d.appendChild(E('div','small muted',sub(c)));
  if(L==='en'&&c.why_en)d.appendChild(E('div','small',S('card_why')+colon+c.why_en));
  else if(L==='en'&&c.why_zh)d.appendChild(trLine('small',S('card_why')+colon,c.why_zh,c.why_zh_tr,c.why_zh_x));
  else if(c.why_zh)d.appendChild(E('div','small',S('card_why')+colon+c.why_zh));
  if(c.what)d.appendChild(trLine('small',S('card_what')+colon,c.what,c.what_tr,c.what_x));
  d.appendChild(quote(c.quote));d.appendChild(E('div','small muted',S('card_explain')));
  var bt=E('div','btns');[['yes','yes'],['no','no'],['?','unsure']].forEach(function(p){var b=E('button',a.v===p[0]?'on':null,S(p[1]));
   b.onclick=function(){A[c.n]={v:p[0],chip:null};render();};bt.appendChild(b);});
  var cl=E('button',null,S('clear'));cl.onclick=function(){delete A[c.n];render();};bt.appendChild(cl);d.appendChild(bt);
  var chips=a.v==='yes'?['a','b']:(a.v==='no'?Object.keys(D.chips||{}).filter(function(k){return k!=='a'&&k!=='b';}).sort():[]);chips=chips.filter(function(k){return c.tokens&&c.tokens[k]&&D.chips[k];});
  if(chips.length){d.appendChild(E('div','small muted',S(a.v==='yes'?'yes_more':'no_more')));var cb=E('div','btns');
   chips.forEach(function(k){var b=E('button',a.chip===k?'on':null,k+' · '+(D.chips[k][L]||D.chips[k].zh));
    b.onclick=function(){A[c.n]={v:a.v,chip:(a.chip===k?null:k)};render();};cb.appendChild(b);});d.appendChild(cb);}
  m.appendChild(d);});
 if((D.unverified||[]).length){var ud=E('details','unv');ud.appendChild(E('summary',null,fmt(S('unverified_title'),{n:D.unverified_total})));
  var SK={contradicted:'st_contradicted',skipped_budget:'st_skipped_budget',failed:'st_failed',uncertain:'st_failed',no_excerpt:'st_no_excerpt',insufficient:'st_insufficient'};
  var groups=D.unverified_groups||[];var byKey={};D.unverified.forEach(function(u){var k=SK[u.status]||'st_other';(byKey[k]=byKey[k]||[]).push(u);});
  if(!groups.length)groups=Object.keys(byKey).map(function(k){return {key:k,n:byKey[k].length};});
  groups.forEach(function(gr){var p=E('p','small');p.appendChild(tag('gap'));p.appendChild(T(fmt(S('unverified_group'),{n:gr.n,why:S(gr.key)})));ud.appendChild(p);
   var us=byKey[gr.key]||[];if(us.length)ud.appendChild(E('div','small muted names',us.map(function(u){return nmFull(u)+(u.ticker?' '+u.ticker:'');}).join(L==='zh'?'、':', ')+(us.length<gr.n?' …':'')));});
  m.appendChild(ud);}
 if((D.excluded||[]).length){m.appendChild(E('h2',null,fmt(S('excluded_title'),{n:D.excluded.length})));
  D.excluded.forEach(function(x){var p=E('div','small');p.appendChild(tag('user'));p.appendChild(T(nmFull(x)+' '+(x.ticker||'')));m.appendChild(p);});}
 if((D.gaps||[]).length){m.appendChild(E('h2',null,S('gaps_title')));D.gaps.forEach(function(g){var p=E('p','small');p.appendChild(tag('gap'));
  p.appendChild(T(L==='en'?g.text_en:g.text_zh));m.appendChild(p);});}
 m.appendChild(E('footer',null,fmt(S('footer'),{run:D.run_id,deck:D.deck_id||'-',ver:D.version_tool})));
 document.body.className=(D.cards||[]).length?'hasbar':'';if(!(D.cards||[]).length)return;
 var bar=E('div','bar');var bi=E('div','in');bi.appendChild(E('span','small lbl',S('bar_label')));var ta=E('textarea');ta.readOnly=true;ta.rows=2;
 var line=answerLine();ta.value=line||'';ta.placeholder=(D.cards||[]).length?S('bar_empty'):S('no_cards');bi.appendChild(ta);
 var cp=E('button',null,S('copy'));var msg=E('span','small muted msg');cp.disabled=!line;
 cp.onclick=function(){var text=S('copy_prefix')+line;function ok(){cp.textContent=S('copied');}
  function manual(){ta.focus();ta.select();try{if(document.execCommand('copy')){ok();return;}}catch(e){}msg.textContent=S('copy_failed');}
  if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(text).then(ok,manual);}else{manual();}};
 bi.appendChild(cp);bi.appendChild(msg);bar.appendChild(bi);app.appendChild(bar);
 if(Q){var s2=document.querySelector('input[type=search]');if(s2){s2.focus();}}
}
render();
})();
"""


CHANGE_TEXT = {"zh": {"answers": "应用了你的 {n} 个回答后重排", "fetch": "补抓 {n} 家年报后重排",
                      "fill": "补了 {n} 家公司简介后重排", "fill_fetch": "补了 {m} 家公司简介、补抓 {n} 家年报后重排",
                      "rerun": "重新筛选"},
               "en": {"answers": "re-ranked with your {n} answers", "fetch": "re-ranked after fetching {n} annual "
                      "reports", "fill": "re-ranked after filling profiles of {n} companies",
                      "fill_fetch": "re-ranked after filling profiles of {m} companies and fetching {n} annual reports",
                      "rerun": "screened again"}}


def change_of(result: dict[str, Any], lineage: dict[str, Any] | None = None) -> dict[str, str] | None:
    """What this version changed against the previous one, in both languages (None for a first run): the human's
    card answers, an on-demand annual-report fetch (the update pass), a profile fill (l1_new), a fill followed by
    its fetch pass (lineage prev_fill: the profiles the previous version filled) or a plain rerun."""
    params = result.get("params") or {}
    if not params.get("from_run") and not result.get("supersedes"):
        return None
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
    lg = "en" if data.get("lang") == "en" else "zh"
    S = STRINGS[lg]
    title = _html.escape(f"{S['title']} · {data.get('headline') or data.get('idea') or ''}", quote=False)
    text = _html.escape(render_text(data), quote=False)
    return ("<!DOCTYPE html>\n"
            f'<html lang="{"en" if lg == "en" else "zh-CN"}">\n<head>\n<meta charset="utf-8">\n'
            f'<meta http-equiv="Content-Security-Policy" content="{CSP}">\n'
            '<meta name="referrer" content="no-referrer">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f"<title>{title}</title>\n<style>{CSS}</style>\n</head>\n<body>\n"
            f'<div id="app"><noscript><p>{S["no_js"]}</p><pre class="plain">{text}</pre></noscript></div>\n'
            f'<script type="application/json" id="data">{_json_for_script(data)}</script>\n'
            f"<script>{JS}</script>\n</body>\n</html>\n")


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
    shown = [r.get("security_id") for r in listed + list((deck or {}).get("cards") or [])[:MAX_CARDS]
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


def page_data(cfg, result: dict[str, Any], deck: dict[str, Any] | None, *, lang: str = "zh",
              extra: dict[str, Any] | None = None, strict: bool = False) -> dict[str, Any]:
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
    ex.setdefault("ondemand", ((result.get("layers") or {}).get("fetch")))   # the on-demand fetch, if one ran
    if "totals" not in ex and result.get("idea"):
        with contextlib.suppress(Exception):   # the idea's cumulative cost and time (quickstart.idea_totals)
            from . import quickstart
            ex["totals"] = quickstart.idea_totals(cfg, result["idea"], wait_s=5.0)
    data = build_page_data(result, deck, lang=lang, lineage=lineage, descriptions=descs, country_of=countries,
                           extra=ex, local_names=local)
    if busy:
        data["store_busy"] = True
        return data
    return with_translations(cfg, data, strict=strict)


def write_page(cfg, out_dir: str | Path, result: dict[str, Any], deck: dict[str, Any] | None, *, lang: str = "zh",
               extra: dict[str, Any] | None = None, stable: bool = True,
               warn: Callable[[str], None] | None = None) -> tuple[Path | None, dict[str, Any] | None]:
    """Write <out_dir>/page.html (and the stable copy <home>/pages/<idea_key>.html when this is the idea's newest
    run, see _newest_for_stable). Returns (path, data), or (None, None) after a warning when it could not be written
    (a configured secret in it, an I/O error)."""
    def say(msg: str) -> None:
        (warn or (lambda m: print(m, file=sys.stderr)))(msg)
    try:
        from . import ops
        from . import translations
        out = Path(out_dir)
        path = out / "page.html"
        data = page_data(cfg, result, deck, lang=lang, extra=extra)
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
            sp = stable_path(cfg, result["idea"])
            sp.parent.mkdir(parents=True, exist_ok=True)
            tmp = sp.with_suffix(f".{os.getpid()}.tmp")
            shutil.copyfile(path, tmp)
            os.replace(tmp, sp)
            owner = _stable_owner_path(cfg, result["idea"])
            owner.write_text(json.dumps({"run_id": result.get("run_id"), "started_at": result.get("started_at")}),
                             encoding="utf-8")
        if busy:
            data["store_busy"] = True
        return path, data
    except Exception as e:  # noqa: BLE001 - a page failure is a warning, never an exit code
        say(f"warning: result page not written ({type(e).__name__}: {str(e)[:200]})")
        return None, None


def can_open_browser(env: dict[str, str] | None = None, platform: str | None = None,
                     browser_name: Callable[[], str | None] | None = None) -> bool:
    """macOS / Windows, or Linux with a display and a browser that is not a console one; never under CI."""
    env = os.environ if env is None else env
    platform = platform or sys.platform
    if env.get("CI"):
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
    rows = (data or {}).get("rows") or []
    return {"listed": len(rows), "annual_report": sum(1 for r in rows if r.get("evidence") == "annual_report"),
            "profile_only": sum(1 for r in rows if r.get("evidence") == "profile"),
            "edge": sum(1 for r in rows if r.get("edge") or "edge" in (r.get("badges") or [])),
            "no_mention": sum(1 for r in rows if "no_mention" in (r.get("badges") or [])),
            "cards": len((deck or {}).get("cards") or [])}


def top_rows(data: dict[str, Any] | None, n: int = 10) -> list[dict[str, Any]]:
    """The first n rows for the agent to relay in chat (quickstart JSON 'top'), in the page's language: 'name' is the
    name the page shows (on a Chinese page the official Chinese short name, else the agent's translation, else the
    English name), 'name_en' the English one; 'one_line' what the page shows (the agent's translation when there is
    one: 'one_line_translated' true, the verbatim text in 'one_line_original'; 'one_line_needs_translation' true when
    it is still in another language); 'excerpt_mentions_idea' false = none of the text the AI read has the idea's
    words (say so, it is a gap; null: nothing to check); 'edge' borderline; 'user' the user answered this company;
    'verdict_from_user' it is listed only because of that answer (the AI did not confirm it from the text)."""
    lang = (data or {}).get("lang") or "zh"
    out = []
    for r in ((data or {}).get("rows") or [])[:n]:
        v = r.get("verdict") or "partial"
        q = r.get("quote") or {}
        out.append({"rank": r.get("rank"), "name": display_name(r, lang), "name_en": r.get("name"),
                    "name_zh": r.get("name_zh"), "name_translated": bool(lang == "zh" and not r.get("name_zh")
                                                                       and r.get("name_tr")),
                    "ticker": r.get("ticker"), "country": r.get("country"),
                    "country_zh": r.get("country_zh"),
                    "verdict_words_zh": STRINGS["zh"].get(f"verdict_{v}", v),
                    "verdict_words_en": STRINGS["en"].get(f"verdict_{v}", v),
                    "evidence_kind": r.get("evidence"), "one_line": shown_text(r, "one_line"),
                    "one_line_original": r.get("one_line") if r.get("one_line_tr") else None,
                    "one_line_translated": bool(r.get("one_line_tr")),
                    "one_line_needs_translation": bool(r.get("one_line_x") and not r.get("one_line_tr")),
                    "excerpt_mentions_idea": q.get("mentions"), "edge": bool(r.get("edge")),
                    "user": bool(r.get("user")), "verdict_from_user": bool(r.get("user_only"))})
    return out


def iter_strings_keys() -> Iterable[tuple[str, set[str]]]:
    return ((lang, set(t)) for lang, t in STRINGS.items())
