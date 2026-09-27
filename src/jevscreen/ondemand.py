"""On-demand official annual-report text for layer-2 candidates that were read on a profile only.

After a screen (phase 1: its report is already written), the companies whose layer-2 input was the company profile
get their newest annual report fetched live from the official source, and the report is updated from the stored
L1 answers (screen(from_run=..., supersedes=...), usually $0: unchanged L2 items are Jev cache hits).

Rules:
- The library never fetches: screen.screen() has no network side effect. Only the CLI (screen --fetch-docs auto,
  jevscreen fetch-docs) calls plan() / launch().
- plan() is local and read-only (one short read-only session): every company gets either a source or a skip reason
  BEFORE any request (already_stored, edinet_pack, no_adapter, no_pdf_reader, no_key_sec, no_key_dart, mops_off,
  disabled, source_paused, known_failure, deferred_cap; store_busy when the store is locked).
- launch() runs ONE child process per source (`python -m jevscreen.ondemand child`), never threads: each child calls
  that adapter's own sync(codes=..., on_company=...) under the source's rate-budget lock (non-reentrant: another
  process using it -> 'source_busy', zero requests), so the adapter's polite limiter, cooldown marker on a block,
  stop-on-first-block, consecutive-error stop and size / deadline caps all stay in force. A block stops that source
  only. The parent opens NO DuckDB connection while a child is alive; each child writes documents / crawl_state /
  runs exactly as the adapter's own sync does.
- Time budget: at the deadline the parent sends SIGTERM (the child flushes what it has and records 'interrupted'),
  SIGKILL GRACE_S later as a last resort ('abandoned'; committed rows stay safe). Each child also stops itself at the
  deadline, so an orphan never runs on.
- SEC sends nothing without a sec-email; DART nothing without an OpenDART key; MOPS nothing without a recorded
  `consent set mops-annual yes`; CNINFO / BSE / MOPS nothing without a PDF reader. EDINET is not fetched here
  (Japanese text comes from the open pack or the user's own sync-edinet).
- Every company that stays profile-only has a reason code (REASONS, zh + en) for the report's gap section; raw adapter
  notes stay in the JSON only. Only the run's language is printed.
No adapter is imported at module level (and no duckdb): doctor can import this module without the store.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import importlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


@dataclass(frozen=True)
class Source:
    key: str                     # sec | cninfo | bse | mops | dart
    source_id: str               # documents.source_id the adapter writes
    module: str                  # jevscreen.sources.<x>
    budget: str                  # guard.budget_lock name (the adapter re-enters it)
    commands: tuple[str, ...]    # runs.command names of that sync; commands[0] is the cooldown-marker command
    id_type: str                 # identifiers.id_type the adapter maps lines to
    needs_pdf: bool
    needs: str | None            # 'sec_email' | 'opendart' | 'consent:mops-annual' | None
    cap: int | None              # companies per invocation (screen and fetch-docs alike); None = no cap
    s_per_company: float         # provisional; <home>/ondemand/speeds.json overrides once measured
    list_s: float                # one-off cost before the first company (tickers file, scrip list, corpCode)
    mb_per_company: float        # provisional download size
    sync_kwargs: tuple[tuple[str, Any], ...] = ()


SOURCES: dict[str, Source] = {s.key: s for s in (
    Source("sec", "sec_filing_text", "jevscreen.sources.sec_edgar", "sec.gov",
           ("sync-sec", "sync sec_edgar", "sec_edgar.sync", "sync sec_filing_text"), "sec_cik", False, "sec_email",
           None, 1.25, 0.8, 1.5),
    Source("cninfo", "cninfo_annual_report", "jevscreen.sources.cninfo", "cninfo",
           ("sync-cninfo", "sync cninfo", "cninfo.sync", "sync cninfo_annual_report"), "cninfo_orgid", True, None,
           None, 2.5, 0.0, 4.0, (("kind", "summary"),)),
    Source("bse", "bse_annual_report", "jevscreen.sources.bse", "bse", ("sync-bse",), "bse_scrip_code", True, None,
           40, 2.5, 4.2, 8.0),
    Source("mops", "mops_annual_report", "jevscreen.sources.mops", "mops",
           ("sync-mops", "sync mops", "mops.sync", "sync mops_annual_report"), "mops_co_id", True,
           "consent:mops-annual", 8, 18.0, 0.0, 5.0, (("mode", "annual"),)),
    Source("dart", "dart_business_report", "jevscreen.sources.dart", "dart",
           ("sync-dart", "sync dart", "dart.sync", "sync dart_business_report"), "dart_corp_code", False, "opendart",
           20, 7.5, 3.0, 1.0, (("mode", "web"),)),
)}
ID_ROUTE: dict[str, str] = {"sec_cik": "sec", "cninfo_orgid": "cninfo", "bse_scrip_code": "bse", "mops_co_id": "mops",
                            "dart_corp_code": "dart", "edinet_code": "edinet"}
FETCH_TIME_DEFAULT_S = 120.0          # screen --fetch-time; fetch-docs --time default is FETCH_DOCS_TIME_S
FETCH_DOCS_TIME_S = 300.0
FETCH_TIME_MAX_S = 1800.0
GRACE_S = 20.0                        # after SIGTERM, before SIGKILL
HEARTBEAT_S = 15.0
POLL_S = 1.0
UPDATE_BUDGET_DEFAULT = 0.05
PLAN_WAIT_S, OUTCOME_WAIT_S = 5.0, 10.0
NEG_CACHE_DAYS: dict[str, int] = {"no_annual_filing": 14, "no_annual_report": 14, "form_not_supported": 30,
                                  "extract_failed": 30, "error": 1}
# an adapter skip (skipped_unchanged / skipped_current / skipped_known_failed: the newest report was tried before and
# is still unreadable) writes no crawl_state, so the parent remembers it in <home>/ondemand/skips.json
SKIP_CACHE_DAYS = 14
SLOW_FACTOR, SLOW_AFTER = 3.0, 3      # "slow from here": the first 3 companies took > 3x the estimate
SPEED_ALPHA, SPEED_MIN_EVENTS = 0.3, 3
MOPS_TOPIC = "mops-annual"
SEC_ASK_TOPIC = "sec-email-ask"       # a recorded 'no': the sec_email question is not asked again
TESTING_ENV, MODULES_ENV = "JEVSCREEN_TESTING", "JEVSCREEN_ONDEMAND_MODULES"
CHILD_EXIT = {"ok": 0, "blocked": 2, "source_busy": 4, "interrupted": 130}

# ---------------------------------------------------------------------------------------------------------------
# Text (zh / en). Only the run's language is printed (lang_of(idea)).

LABEL: dict[str, tuple[str, str]] = {"sec": ("美国 SEC", "US SEC"), "cninfo": ("A股 巨潮", "China CNINFO"),
                                     "bse": ("印度 BSE", "India BSE"), "mops": ("台湾 MOPS", "Taiwan MOPS"),
                                     "dart": ("韩国 DART", "Korea DART"), "edinet": ("日本 EDINET", "Japan EDINET")}
_PIP = "python3 -m pip install pypdf"
_LATER = "jevscreen fetch-docs latest"
REASONS: dict[str, dict[str, Any]] = {
    "fetched": {"zh": "本次新抓到年报原文", "en": "Annual report fetched now", "short_zh": "本次新抓",
                "short_en": "fetched now", "next_command": None, "ask_human": False},
    "stored_since": {"zh": "年报原文是这次筛选之后存到本地的，更新结果时会读", "en": "Annual report stored after this run "
                     "read the profile; read when the results are updated", "short_zh": "之前已抓到",
                     "short_en": "stored earlier", "next_command": None, "ask_human": False},
    "update_pending": {"zh": "年报原文已存到本地，但结果还没按它更新", "en": "Annual report stored, but the results are not "
                       "updated with it yet", "short_zh": "已抓到，待更新", "short_en": "stored, not re-read yet",
                       "next_command": "jevscreen fetch-docs latest", "ask_human": False},
    "already_stored": {"zh": "本地有年报原文，但里面没读出可用的业务内容", "en": "Annual report stored, but no usable business "
                       "text was found in it", "short_zh": "年报无可用内容", "short_en": "no usable text in the report",
                       "next_command": None, "ask_human": False},
    "no_adapter": {"zh": "该市场（{market}）暂时没有官方年报来源", "en": "No official annual-report source for this market yet "
                   "({market})", "zh_min": "该市场暂时没有官方年报来源",
                   "en_min": "No official annual-report source for this market yet", "short_zh": "市场暂无来源",
                   "short_en": "no source for their market",
                   "next_command": None, "ask_human": False},
    "edinet_pack": {"zh": "日本年报来自开放数据包，这家不在包里", "en": "Japanese reports come from the open pack; this company "
                    "is not in it", "short_zh": "不在开放数据包里", "short_en": "not in the open pack",
                    "next_command": None, "ask_human": False},
    "no_pdf_reader": {"zh": "没装读 PDF 的组件，A股/印度/台湾年报暂时读不了", "en": "No PDF reader installed; China/India/Taiwan "
                      "reports can't be read yet", "short_zh": "没装 PDF 组件", "short_en": "no PDF reader",
                      "next_command": _PIP, "ask_human": False},
    "no_key_sec": {"zh": "未设置 SEC 联系邮箱", "en": "No SEC contact email set", "short_zh": "未设置 SEC 邮箱",
                   "short_en": "no SEC email", "next_command": "jevscreen keys set sec-email", "ask_human": True},
    "no_key_dart": {"zh": "韩国年报需要 OpenDART 免费密钥（默认关闭）", "en": "Korean reports need a free OpenDART key (off by "
                    "default)", "short_zh": "没有 OpenDART 密钥", "short_en": "no OpenDART key",
                    "next_command": "jevscreen keys set opendart", "ask_human": True},
    "mops_off": {"zh": "台湾年报下载未开启", "en": "Taiwan annual-report downloads are off", "short_zh": "台湾下载未开启",
                 "short_en": "Taiwan downloads off", "next_command": None, "ask_human": True},
    "disabled": {"zh": "这次没有选这个来源（--sources）", "en": "Source not selected this time (--sources)",
                 "short_zh": "未选来源", "short_en": "source not selected", "next_command": None, "ask_human": False},
    "not_mapped": {"zh": "官方来源的公司名单里找不到它", "en": "Not in the official source's company list",
                   "short_zh": "官方名单里没有", "short_en": "not in the official list", "next_command": None,
                   "ask_human": False},
    "no_filing": {"zh": "官方来源没有它的年报", "en": "The official source lists no annual report", "short_zh": "没有年报",
                  "short_en": "no annual report listed", "next_command": None, "ask_human": False},
    "form_not_supported": {"zh": "它交的是暂不支持的年报格式（如 40-F）", "en": "Filed in a format not supported yet "
                           "(e.g. 40-F)", "short_zh": "格式暂不支持", "short_en": "format not supported",
                           "next_command": None, "ask_human": False},
    "extract_failed": {"zh": "找到年报，但没能读出业务章节（{note}）", "en": "Report found, business section not read ({note})",
                       "short_zh": "年报读不出", "short_en": "report unreadable",
                       "next_command": f"{_LATER} --retry-failed", "ask_human": False},
    "known_failure": {"zh": "{date} 试过：{note}；{days} 天内不再自动重试", "en": "Tried {date}: {note}; not retried "
                      "automatically for {days} days", "zh_min": "之前试过没成功：{note}", "en_min": "Failed before: {note}",
                      "short_zh": "之前试过没成功", "short_en": "failed before",
                      "next_command": f"{_LATER} --retry-failed", "ask_human": False},
    "deferred_time": {"zh": "时间到了没轮到", "en": "Out of time this run", "short_zh": "时间到",
                      "short_en": "out of time", "next_command": _LATER, "ask_human": False},
    "deferred_cap": {"zh": "{source} 每次最多抓 {cap} 家，其余下次继续", "en": "{source}: at most {cap} per run; the rest "
                     "next time", "zh_min": "每次抓取有上限，其余下次继续", "en_min": "Per-run cap reached; the rest next time",
                     "short_zh": "超过每次上限", "short_en": "over the per-run cap",
                     "next_command": _LATER, "ask_human": False},
    "source_paused": {"zh": "该网站 {when} 暂时拒绝过访问。这不是你的操作问题；已自动暂停，24 小时后自动恢复，无需处理",
                      "en": "The site refused access at {when}. Not something you did; paused automatically, resumes "
                      "after 24 h, nothing to do",
                      "zh_min": "该网站最近暂时拒绝过访问。这不是你的操作问题；已自动暂停，24 小时后自动恢复，无需处理",
                      "en_min": "The site refused access recently. Not something you did; paused automatically, "
                      "resumes after 24 h, nothing to do", "short_zh": "网站暂停中", "short_en": "source paused",
                      "next_command": None, "ask_human": False},
    "blocked": {"zh": "本次抓取时被网站暂时拒绝，已自动停止该来源，24 小时后自动恢复，无需处理", "en": "Refused by the site during "
                "this run; that source stopped automatically and resumes after 24 h, nothing to do",
                "short_zh": "网站拒绝", "short_en": "refused by the site", "next_command": None, "ask_human": False},
    "source_busy": {"zh": "另一个 jevscreen 任务正在用这个来源", "en": "Another jevscreen task is using this source",
                    "short_zh": "来源被占用", "short_en": "source busy", "next_command": None, "ask_human": False},
    "store_busy": {"zh": "数据库正被另一个 jevscreen 任务使用（例如后台的 crawl-descriptions），这次先不补抓",
                   "en": "The database is in use by another jevscreen task (e.g. a background crawl-descriptions); "
                   "fetch skipped this time", "short_zh": "数据库被占用", "short_en": "database busy",
                   "next_command": None, "ask_human": False},
    "error": {"zh": "网络错误，明天会自动重试", "en": "Network error; retried automatically tomorrow", "short_zh": "网络错误",
              "short_en": "network error", "next_command": None, "ask_human": False},
    "unresolved": {"zh": "没抓到，原因没有记录（详见 fetch/{src}.log）", "en": "Not fetched; no reason recorded (see "
                   "fetch/{src}.log)", "zh_min": "没抓到，原因没有记录", "en_min": "Not fetched; no reason recorded",
                   "short_zh": "原因未记录", "short_en": "no reason recorded", "next_command": None,
                   "ask_human": False},
}
# reasons that are not a gap: the company reads an annual report once the results are updated (already_stored is a
# gap: the document was there when the run read the profile, so it holds no usable text)
NOT_GAPS = frozenset({"fetched", "stored_since"})
DEFERRED = frozenset({"deferred_time", "deferred_cap"})
# raw adapter note prefix -> plain phrase (longest prefix first); the raw note stays in the JSON only
NOTE_TEXT: dict[str, tuple[str, str]] = {
    "pdf_parse_failed:no PDF backend": ("没装读 PDF 的组件", "no PDF reader installed"),
    "too_large": ("年报文件太大（超过 {n} MB）", "report file too large (over {n} MB)"),
    "newest_year_unparsed": ("最新年报的年份读不出来", "newest report's year unreadable"),
    "section_too_short": ("业务章节太短", "business section too short"),
    "pdf_parse_failed": ("PDF 读不出文字（可能是扫描件）", "no text in the PDF (possibly scanned)"),
    "deadline": ("下载超时", "download timed out"),
    "skipped_": ("最新年报之前已下载过，但读不出业务章节", "the newest report was downloaded before but could not be read"),
    "": ("其他原因", "other reason"),
}
# crawl / skip status -> reason (outcomes); 'ok' without a readable document is extract_failed
STATUS_TO_REASON: dict[str, str] = {
    "ok": "extract_failed", "no_annual_filing": "no_filing", "no_annual_report": "no_filing", "no_basic": "no_filing",
    "no_csv": "no_filing", "form_not_supported": "form_not_supported", "extract_failed": "extract_failed",
    "error": "error", "blocked": "blocked", "skipped_unchanged": "known_failure", "skipped_current": "known_failure",
    "skipped_known_failed": "known_failure",
}
SKIP_STATUSES = ("skipped_unchanged", "skipped_current", "skipped_known_failed")
QUESTIONS: dict[str, dict[str, Any]] = {
    "sec_email": {
        "human_question_zh": "美国证监会（SEC）要求下载年报的人在请求里写上名字和邮箱。这些信息只发给 sec.gov，不发给别人，可以用一个专门的"
                             "邮箱。设置后，美国公司可以读到年报原文，而不只是公司简介。要设置吗？",
        "human_question_en": "The US SEC requires anyone downloading filings to state a name and email in each request. "
                             "It goes only to sec.gov, never elsewhere, and a dedicated address is fine. With it, US "
                             "companies are read from their annual reports instead of a short profile. Set it up?",
        "record_answer_commands": ["jevscreen keys set sec-email", f"jevscreen consent set {SEC_ASK_TOPIC} no"]},
    "mops_annual": {
        "human_question_zh": "台湾有 {n} 家公司可以从台湾证交所的 MOPS 网站慢速下载年报：每次最多 8 家，每次请求间隔 1.5 秒。这个网站的 "
                             "robots.txt 不欢迎自动下载，所以默认关闭。要开启吗？",
        "human_question_en": "{n} Taiwan companies could get annual reports from the exchange's MOPS site, slowly (at "
                             "most 8 per run, 1.5 s between requests). The site's robots.txt discourages automated "
                             "downloads, so this is off by default. Turn it on?",
        "record_answer_commands": [f"jevscreen consent set {MOPS_TOPIC} yes", f"jevscreen consent set {MOPS_TOPIC} no"]},
}
COUNTRY_ZH: dict[str, str] = {
    "United States": "美国", "China": "中国", "Hong Kong": "香港", "Taiwan": "台湾", "Japan": "日本", "Korea": "韩国",
    "South Korea": "韩国", "India": "印度", "Indonesia": "印尼", "Vietnam": "越南", "Thailand": "泰国",
    "Malaysia": "马来西亚", "Singapore": "新加坡", "Philippines": "菲律宾", "United Kingdom": "英国", "Germany": "德国",
    "France": "法国", "Italy": "意大利", "Spain": "西班牙", "Netherlands": "荷兰", "Switzerland": "瑞士", "Sweden": "瑞典",
    "Norway": "挪威", "Denmark": "丹麦", "Finland": "芬兰", "Belgium": "比利时", "Austria": "奥地利", "Ireland": "爱尔兰",
    "Poland": "波兰", "Israel": "以色列", "Turkey": "土耳其", "Saudi Arabia": "沙特", "United Arab Emirates": "阿联酋",
    "Australia": "澳大利亚", "New Zealand": "新西兰", "Canada": "加拿大", "Mexico": "墨西哥", "Brazil": "巴西",
    "Chile": "智利", "Argentina": "阿根廷", "South Africa": "南非", "Greece": "希腊", "Portugal": "葡萄牙",
    "Cayman Islands": "开曼", "Bermuda": "百慕大", "Luxembourg": "卢森堡", "Qatar": "卡塔尔", "Kuwait": "科威特",
}


# kanji-only Japanese: shinjitai forms used by neither simplified nor traditional Chinese, or a traditional-only form
# next to a simplified-only one (半導体製造装置: 導 製 + 体 装). Chinese in either script has neither.
_JA_ONLY = frozenset("変気険験読売続転伝図県対様楽薬済経関発実観歩戦駅営価拡鉄圧")
_TRAD_FORMS = frozenset("導製業電車機銀開動術設備療資網質貿")
_SIMP_FORMS = frozenset("体装国会医万学区当点断数条号声")


def lang_of(idea: str | None) -> str:
    """'zh' when the idea is written in Chinese characters (Han, no kana / hangul, not kanji-only Japanese), else
    'en'. A short idea counts (screen.detect_language needs 20 script characters; a 5-character 人形机器人 must still
    read Chinese)."""
    text = idea or ""
    han = len(re.findall(r"[㐀-䶿一-鿿豈-﫿]", text))
    other = len(re.findall(r"[぀-ヿㇰ-ㇿｦ-ﾟᄀ-ᇿ㄰-㆏가-힯]", text))
    chars = set(text)
    japanese = bool(chars & _JA_ONLY) or bool(chars & _TRAD_FORMS and chars & _SIMP_FORMS)
    return "zh" if han and not other and not japanese else "en"


def label(key: str | None, lang: str) -> str:
    if key is None:
        return "-"
    zh, en = LABEL.get(key, (key, key))
    return zh if lang == "zh" else en


def market_name(country: str | None, lang: str) -> str:
    c = (country or "").strip() or ("未知" if lang == "zh" else "unknown")
    return COUNTRY_ZH.get(c, c) if lang == "zh" else c


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "?"


def note_text(note: str | None, lang: str) -> str:
    """The plain phrase of a raw adapter note (NOTE_TEXT, longest matching prefix)."""
    raw = str(note or "").strip()
    first = raw.split(";", 1)[0]
    for prefix in sorted(NOTE_TEXT, key=len, reverse=True):
        if prefix and first.startswith(prefix) or not prefix:
            zh, en = NOTE_TEXT[prefix]
            n = "?"
            m = re.search(r"(\d+)\s*(KB|MB)?", first) if prefix == "too_large" else None
            if m:
                n = str(max(1, round(int(m.group(1)) / 1024))) if (m.group(2) or "KB") == "KB" else m.group(1)
            return (zh if lang == "zh" else en).format(n=n)
    return NOTE_TEXT[""][0 if lang == "zh" else 1]


def reason_text(code: str, lang: str, **kw: Any) -> str:
    """The reason's sentence; when a value it names is missing or None, its shorter form without that clause (never
    'None' in user text)."""
    r = REASONS.get(code) or REASONS["unresolved"]
    if "note" in kw:
        kw["note"] = note_text(kw["note"], lang)
    tpl = r["zh"] if lang == "zh" else r["en"]
    if any(kw.get(k) is None for k in re.findall(r"\{(\w+)\}", tpl)):
        tpl = r.get("zh_min" if lang == "zh" else "en_min") or tpl
    return tpl.format_map(_SafeDict({k: v for k, v in kw.items() if v is not None}))


def short_reason(code: str, lang: str) -> str:
    r = REASONS.get(code) or REASONS["unresolved"]
    return r["short_zh"] if lang == "zh" else r["short_en"]


# ---------------------------------------------------------------------------------------------------------------
# Routing and readiness (local, no request)

class RealAdaptersRefused(RuntimeError):
    """JEVSCREEN_TESTING=1 without JEVSCREEN_ONDEMAND_MODULES: a test was about to run a real adapter (network)."""


def _real_adapters_refused() -> bool:
    return os.environ.get(TESTING_ENV) == "1" and not os.environ.get(MODULES_ENV)


def adapter_module(key: str) -> str:
    """The module the child runs for a source. Under JEVSCREEN_TESTING=1 the real adapters are refused unless
    JEVSCREEN_ONDEMAND_MODULES maps the source to a fake (a network kill switch for tests)."""
    if _real_adapters_refused():
        raise RealAdaptersRefused(f"{TESTING_ENV}=1: the real {key} adapter is refused (set {MODULES_ENV})")
    return _modules()[key]


def _modules() -> dict[str, str]:
    """Adapter module per source. Tests may override it (JEVSCREEN_ONDEMAND_MODULES, only with JEVSCREEN_TESTING=1)."""
    out = {k: s.module for k, s in SOURCES.items()}
    if os.environ.get(TESTING_ENV) == "1" and os.environ.get(MODULES_ENV):
        with contextlib.suppress(ValueError, TypeError):
            out.update({str(k): str(v) for k, v in json.loads(os.environ[MODULES_ENV]).items() if k in SOURCES})
    return out


def _venue_key(line: Mapping[str, Any]) -> str | None:
    from .sources import bse, cninfo, dart, edinet, mops, sec_edgar
    ex = str(line.get("exchange") or "").strip().upper()
    if cninfo.is_cn_line(line):
        return "cninfo"
    if ex in mops.TW_VENUES:
        return "mops"
    if ex in dart.KR_VENUES:
        return "dart"
    if bse.is_in_line(line):
        return "bse"
    if ex in sec_edgar.US_VENUES:
        return "sec"
    if ex in edinet.JP_VENUES:
        return "edinet"
    return None


def route(lines: Sequence[Mapping[str, Any]], ids: Mapping[str, Mapping[str, str]]
          ) -> tuple[str | None, str | None]:
    """(source key, None) or (None, 'edinet_pack' | 'no_adapter') for one company (all its lines, primary first).

    First an identifier of a source's id_type on any of the company's own lines, then the venue of its lines (the
    adapters' own predicates: cninfo.is_cn_line, mops.TW_VENUES, dart.KR_VENUES, bse.is_in_line,
    sec_edgar.US_VENUES; Japanese venues -> edinet_pack). company_key is per ISIN: an A+H or ADR line of the same
    issuer with another ISIN is another company here."""
    for id_type, key in ID_ROUTE.items():
        if any(id_type in (ids.get(ln["security_id"]) or {}) for ln in lines):
            return (None, "edinet_pack") if key == "edinet" else (key, None)
    for ln in lines:
        key = _venue_key(ln)
        if key == "edinet":
            return None, "edinet_pack"
        if key is not None:
            return key, None
    return None, "no_adapter"


def source_codes(key: str, lines: Sequence[Mapping[str, Any]], ids: Mapping[str, Mapping[str, str]]) -> list[str]:
    """The security ids handed to the source's sync(codes=...): the company's lines that belong to that source (its
    id_type, else its venue), so another venue's symbol never matches a code of this source."""
    id_type = SOURCES[key].id_type
    own = [ln["security_id"] for ln in lines if id_type in (ids.get(ln["security_id"]) or {})
           or _venue_key(ln) == key]
    return own or [ln["security_id"] for ln in lines]


def pdf_backends() -> list[str]:
    out = []
    for mod, name in (("fitz", "pymupdf"), ("pypdf", "pypdf")):
        try:
            importlib.import_module(mod)
            out.append(name)
        except Exception:  # noqa: BLE001 - ImportError or a broken install: not usable either way
            pass
    return out


def readiness(cfg: Any) -> dict[str, Any]:
    """Local only: {pdf_reader, backends, sec_email, opendart, mops_consent}. Key values are never returned."""
    from . import consent
    backends = pdf_backends()
    state = consent.get(cfg, MOPS_TOPIC)["state"] if MOPS_TOPIC in consent.TOPICS else "unset"
    ask = consent.get(cfg, SEC_ASK_TOPIC)["state"] if SEC_ASK_TOPIC in consent.TOPICS else "unset"
    return {"pdf_reader": bool(backends), "backends": backends, "sec_email": bool(cfg.sec_user_agent()),
            "opendart": bool(cfg.opendart_api_key()),
            "mops_consent": state if state in ("yes", "no") else None,
            "sec_email_ask": ask if ask in ("yes", "no") else None}


# ---------------------------------------------------------------------------------------------------------------
# Speeds (<home>/ondemand/speeds.json)

def _speeds_path(cfg: Any) -> Path:
    return Path(cfg.home) / "ondemand" / "speeds.json"


def load_speeds(cfg: Any) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_speeds_path(cfg).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def s_per_company(cfg: Any, key: str, speeds: Mapping[str, Any] | None = None) -> float:
    rec = (speeds if speeds is not None else load_speeds(cfg)).get(key) or {}
    v = rec.get("s_per_company_ewma")
    return float(v) if isinstance(v, (int, float)) and v > 0 else SOURCES[key].s_per_company


def record_speed(cfg: Any, key: str, seconds: float, companies: int) -> None:
    if companies < SPEED_MIN_EVENTS or seconds <= 0:
        return
    data = load_speeds(cfg)
    rec = data.get(key) or {}
    x = (seconds - SOURCES[key].list_s) / companies
    x = max(0.05, x)
    old = rec.get("s_per_company_ewma")
    new = x if not isinstance(old, (int, float)) else (1 - SPEED_ALPHA) * old + SPEED_ALPHA * x
    data[key] = {"s_per_company_ewma": round(new, 3), "n": int(rec.get("n") or 0) + companies,
                 "last": round(x, 3)}
    path = _speeds_path(cfg)
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)


def _skips_path(cfg: Any) -> Path:
    return Path(cfg.home) / "ondemand" / "skips.json"


def load_skips(cfg: Any) -> dict[str, dict[str, dict[str, Any]]]:
    """{source key: {company_key: {'at': ISO UTC, 'status': adapter skip status}}}."""
    try:
        data = json.loads(_skips_path(cfg).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record_skips(cfg: Any, oc: Mapping[str, Mapping[str, Any]], now: dt.datetime | None = None) -> None:
    """Remember the companies an adapter skipped this time (outcome status in SKIP_STATUSES), so plan() does not
    queue them again for SKIP_CACHE_DAYS (--retry-failed still does). Entries older than the window are dropped."""
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    hits = [(o.get("source"), ck, o.get("status")) for ck, o in oc.items() if o.get("status") in SKIP_STATUSES]
    if not hits:
        return
    data = load_skips(cfg)
    for key, ck, st in hits:
        if key:
            data.setdefault(str(key), {})[ck] = {"at": now.isoformat(timespec="seconds"), "status": st}
    cut = now - dt.timedelta(days=SKIP_CACHE_DAYS)
    for key in list(data):
        recs = data[key] if isinstance(data[key], dict) else {}
        data[key] = {ck: r for ck, r in recs.items() if (_skip_at(r) or cut) > cut}
    path = _skips_path(cfg)
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)


def _skip_at(rec: Any) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str((rec or {}).get("at")))
    except (ValueError, TypeError, AttributeError):
        return None


# ---------------------------------------------------------------------------------------------------------------
# Plan

@dataclass
class Plan:
    run_id: str
    by_source: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    skipped: list[tuple[str, str | None, str, str | None]] = field(default_factory=list)   # (ck, source, reason, note)
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)   # every profile-only L2 input, by company_key
    order: list[str] = field(default_factory=list)
    est_seconds: float = 0.0
    est_mb: float = 0.0
    readiness: dict[str, Any] = field(default_factory=dict)
    stored_n: int = 0             # L2 inputs of the run that read an annual report
    params: dict[str, Any] = field(default_factory=dict)
    idea: str | None = None
    store_busy: bool = False

    @property
    def planned(self) -> dict[str, int]:
        return {k: len(v) for k, v in self.by_source.items() if v}

    @property
    def n_planned(self) -> int:
        return sum(len(v) for v in self.by_source.values())

    def skip_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for _ck, _src, reason, _note in self.skipped:
            out[reason] = out.get(reason, 0) + 1
        return dict(sorted(out.items()))


def _p_core(probs_json: str | None) -> float | None:
    try:
        p = json.loads(probs_json) if probs_json else {}
        v = p.get("core")
        return float(v) if isinstance(v, (int, float)) else None
    except (ValueError, AttributeError):
        return None


def _runs_block(con, names: Sequence[str], now: dt.datetime, hours: float = 24) -> dict[str, Any] | None:
    ph = ",".join("?" * len(names))
    row = con.execute(f"SELECT started_at, status, note FROM runs WHERE command IN ({ph}) "
                      "ORDER BY started_at DESC LIMIT 1", list(names)).fetchone()
    if not row or row[1] != "blocked" or row[0] is None or now - row[0] >= dt.timedelta(hours=hours):
        return None
    return {"blocked_at": row[0], "source": "runs"}


def _extractor_version(key: str) -> str | None:
    try:
        return getattr(importlib.import_module(_modules()[key]), "EXTRACTOR_VERSION", None)
    except Exception:  # noqa: BLE001
        return None


def neg_cache(state: Mapping[str, Any] | None, extractor: str | None, current_version: str | None,
              now: dt.datetime) -> tuple[str, int] | None:
    """(status, days) when the company's latest crawl status for the source is still in its NEG_CACHE_DAYS window,
    else None. extract_failed is not cached when it failed only for want of a PDF reader (note 'pdf_parse_failed:no
    PDF backend' or extractor '.../none') or with another extractor version than the adapter's current one."""
    if not state:
        return None
    status, at, note = state.get("status"), state.get("last_attempt_at"), str(state.get("note") or "")
    days = NEG_CACHE_DAYS.get(status or "")
    if days is None or not isinstance(at, dt.datetime) or now - at >= dt.timedelta(days=days):
        return None
    if status == "extract_failed":
        ext = str(extractor or "")
        if note.startswith("pdf_parse_failed:no PDF backend") or ext.endswith("/none"):
            return None
        if current_version and ext and ext.split("/", 1)[0] != current_version:
            return None
    return status, days


def _readable(doc: Mapping[str, Any] | None) -> bool:
    p = (doc or {}).get("text_path")
    try:
        return bool(p) and Path(p).stat().st_size > 0
    except OSError:
        return False


def load_run_inputs(con, run_id: str) -> dict[str, Any]:
    """The run's params / idea and its L2 inputs: [{company_key, security_id, profile, p_core, l1_label}]."""
    row = con.execute("SELECT idea, params_json, status, output_dir, started_at FROM screen_runs WHERE run_id = ?",
                      [run_id]).fetchone()
    if row is None:
        raise ValueError(f"unknown screen run {run_id!r}")
    try:
        params = json.loads(row[1] or "{}")
    except ValueError:
        params = {}
    inputs = [{"company_key": ck, "security_id": sid, "profile": src == "profile", "p_core": _p_core(pj),
               "l1_label": l1l, "l1_probs": pj}
              for ck, sid, src, pj, l1l in con.execute(
                  "SELECT r.company_key, r.security_id, r.input_source, l1.probs_json, l1.label "
                  "FROM screen_results r LEFT JOIN screen_results l1 ON l1.run_id = r.run_id "
                  "AND l1.company_key = r.company_key AND l1.layer = 'l1' "
                  "WHERE r.run_id = ? AND r.layer = 'l2' ORDER BY r.company_key", [run_id]).fetchall()]
    return {"idea": row[0], "params": params if isinstance(params, dict) else {}, "status": row[2],
            "output_dir": row[3], "started_at": row[4], "inputs": inputs}


def _is_forced(inp: Mapping[str, Any], params: Mapping[str, Any]) -> bool:
    """A sieve check or pin read by L2 although it missed L1 (fetched first); an L1 miss rescued for its thin
    profile (params l1_rescued) is not, unless it is such a check too (params l2_forced): plan() orders it after
    the L1 passes (_is_rescued)."""
    from . import screen
    if _is_rescued(inp, params):
        return False
    try:
        probs = json.loads(inp.get("l1_probs") or "{}")
    except ValueError:
        probs = {}
    res = {"status": "ok", "label": inp.get("l1_label"), "probs": probs}
    kw = {"adjacent_min": params.get("l1_adjacent_min", screen.L1_ADJACENT_MIN),
          "core_min": params.get("l1_core_min", screen.L1_CORE_MIN)}
    return not screen.l1_passes(res, **kw)


def _is_rescued(inp: Mapping[str, Any], params: Mapping[str, Any]) -> bool:
    """An L1 miss read by L2 only because screen.l1_rescued picked it (not also a sieve check or pin)."""
    ck = inp.get("company_key")
    return ck in (params.get("l1_rescued") or ()) and ck not in (params.get("l2_forced") or ())


def plan(cfg: Any, run_id: str, *, sources: Iterable[str] | None = None, retry_failed: bool = False,
         now: dt.datetime | None = None, wait_s: float = PLAN_WAIT_S, time_s: float = FETCH_TIME_DEFAULT_S) -> Plan:
    """Who is fetched from where, and why every other profile-only L2 input is not (skip reasons in first-match
    order: stored_since (a readable document stored after the run started: the update pass reads it) /
    already_stored (stored before, yet the run read the profile: no usable text), edinet_pack, no_adapter,
    no_pdf_reader, no_key_sec, no_key_dart, mops_off, disabled, source_paused, known_failure (crawl_state window or
    a remembered adapter skip), deferred_cap). ONE read-only session (wait_s); a locked store -> Plan.store_busy.
    No request is made. Raises ValueError for an unknown run."""
    from . import guard, screen, store
    now = now or store.now_utc()
    enabled = set(SOURCES) if sources is None else {s for s in sources if s in SOURCES}
    rd = readiness(cfg)
    pl = Plan(run_id=run_id, readiness=rd)
    try:
        with store.session(cfg, read_only=True, wait_s=wait_s) as con:
            run = load_run_inputs(con, run_id)
            pl.params, pl.idea = run["params"], run["idea"]
            prof = [i for i in run["inputs"] if i["profile"]]
            pl.stored_n = sum(1 for i in run["inputs"] if not i["profile"])
            cks = [i["company_key"] for i in prof]
            lines_by_ck: dict[str, list[dict]] = {}
            if cks:
                for r in con.execute(
                        "SELECT s.company_key, s.security_id, s.exchange, s.symbol, s.isin, s.name, s.country, "
                        "s.is_primary, lm.market_cap_usd FROM securities s LEFT JOIN latest_market lm "
                        "USING (security_id) WHERE list_contains(?::VARCHAR[], s.company_key) "
                        "ORDER BY s.is_primary DESC NULLS LAST, lm.market_cap_usd DESC NULLS LAST, s.security_id",
                        [cks]).fetchall():
                    lines_by_ck.setdefault(r[0], []).append(dict(zip(
                        ("company_key", "security_id", "exchange", "symbol", "isin", "name", "country", "is_primary",
                         "market_cap_usd"), r)))
            sids = sorted({ln["security_id"] for v in lines_by_ck.values() for ln in v})
            ids: dict[str, dict[str, str]] = {}
            states: dict[tuple[str, str], dict] = {}
            if sids:
                for sid, t, v in con.execute("SELECT security_id, id_type, id_value FROM identifiers "
                                             "WHERE list_contains(?::VARCHAR[], security_id)", [sids]).fetchall():
                    ids.setdefault(sid, {})[t] = v
                for src, sid, st, at, note in con.execute(
                        "SELECT source_id, security_id, status, last_attempt_at, note FROM crawl_state "
                        "WHERE list_contains(?::VARCHAR[], security_id)", [sids]).fetchall():
                    states[(src, sid)] = {"status": st, "last_attempt_at": at, "note": note}
            docs = screen.load_documents(con, min_mcap_usd=float(pl.params.get("min_mcap_usd") or 0.0)) if cks else {}
            doc_ids = sorted({d["doc_id"] for d in docs.values() if d.get("doc_id")} if cks else set())
            stored_at = dict(con.execute("SELECT doc_id, fetched_at FROM documents WHERE list_contains(?::VARCHAR[], "
                                         "doc_id)", [doc_ids]).fetchall()) if doc_ids else {}
            extractors: dict[tuple[str, str], str | None] = {}
            if cks:
                idvals = sorted({v for d in ids.values() for v in d.values()})
                for src, sid, ck, cik, ext in con.execute(
                        "SELECT source_id, security_id, company_key, cik, extractor FROM documents WHERE "
                        "list_contains(?::VARCHAR[], security_id) OR list_contains(?::VARCHAR[], company_key) OR "
                        "list_contains(?::VARCHAR[], cik) ORDER BY fetched_at DESC NULLS LAST",
                        [sids, cks, idvals]).fetchall():
                    for key in (sid, ck, cik):
                        if key:
                            extractors.setdefault((src, key), ext)
            paused: dict[str, dict] = {}
            for key, s in SOURCES.items():
                hit = guard.recent_block_marker(cfg, s.commands[0]) or _runs_block(con, s.commands, now)
                if hit:
                    paused[key] = hit
    except store.StoreLocked:
        pl.store_busy = True
        return pl
    versions: dict[str, str | None] = {}
    skips = load_skips(cfg) if not retry_failed else {}
    started = run.get("started_at")
    rows = []
    for inp in prof:
        ck = inp["company_key"]
        lines = lines_by_ck.get(ck) or [{"company_key": ck, "security_id": inp["security_id"], "exchange": None,
                                         "symbol": None, "isin": None, "name": None, "country": None,
                                         "market_cap_usd": None}]
        mcap = max((ln["market_cap_usd"] for ln in lines if ln.get("market_cap_usd") is not None), default=None)
        head = next((ln for ln in lines if ln["security_id"] == inp["security_id"]), lines[0])
        e = {"company_key": ck, "security_id": inp["security_id"], "name": head.get("name"),
             "country": head.get("country"), "exchange": head.get("exchange"), "market_cap_usd": mcap,
             "p_core": inp["p_core"], "forced": _is_forced(inp, pl.params),
             "rescued": _is_rescued(inp, pl.params), "lines": lines}
        pl.entries[ck] = e
        rows.append(e)
    # sieve checks and pins that missed L1 first, then the L1 passes, then the rescued L1 misses
    rows.sort(key=lambda e: (not e["forced"], e["rescued"], -(e["p_core"] or 0.0), -(e["market_cap_usd"] or 0.0),
                             e["security_id"]))
    pl.order = [e["company_key"] for e in rows]
    counts: dict[str, int] = {}
    for e in rows:
        ck, lines = e["company_key"], e["lines"]
        key, why = route(lines, ids)
        e["source"] = key

        def skip(reason: str, note: str | None = None, **extra: Any) -> None:
            e.update(reason=reason, note=note, **extra)
            pl.skipped.append((ck, key, reason, note))
        if _readable(docs.get(ck)):
            at = stored_at.get(docs[ck].get("doc_id"))
            skip("stored_since" if isinstance(at, dt.datetime) and isinstance(started, dt.datetime) and at >= started
                 else "already_stored")
            continue
        if key is None:
            skip(why or "no_adapter")
            continue
        s = SOURCES[key]
        if s.needs_pdf and not rd["pdf_reader"]:
            skip("no_pdf_reader")
            continue
        if s.needs == "sec_email" and not rd["sec_email"]:
            skip("no_key_sec")
            continue
        if s.needs == "opendart" and not rd["opendart"]:
            skip("no_key_dart")
            continue
        if s.needs == f"consent:{MOPS_TOPIC}" and rd["mops_consent"] != "yes":
            skip("mops_off")
            continue
        if key not in enabled:
            skip("disabled")
            continue
        if key in paused:
            skip("source_paused", None, when=str(paused[key].get("blocked_at"))[:16] + " UTC")
            continue
        if not retry_failed:
            if key not in versions:
                versions[key] = _extractor_version(key)
            latest = max((states[(s.source_id, ln["security_id"])] for ln in lines
                          if (s.source_id, ln["security_id"]) in states),
                         key=lambda st: st.get("last_attempt_at") or dt.datetime.min, default=None)
            ext = next((extractors[(s.source_id, k)] for k in
                        [ln["security_id"] for ln in lines] + [ck] + [v for ln in lines
                                                                        for v in (ids.get(ln["security_id"]) or {})
                                                                        .values()]
                        if (s.source_id, k) in extractors), None)
            hit = neg_cache(latest, ext, versions[key], now)
            if hit:
                at = latest.get("last_attempt_at")
                skip("known_failure", latest.get("note") or hit[0], date=at.date().isoformat() if at else "?",
                     days=hit[1], status=hit[0])
                continue
            rec = (skips.get(key) or {}).get(ck)
            sat = _skip_at(rec)
            if sat is not None and now - sat < dt.timedelta(days=SKIP_CACHE_DAYS):
                skip("known_failure", str(rec.get("status") or "skipped_unchanged"), date=sat.date().isoformat(),
                     days=SKIP_CACHE_DAYS, status=rec.get("status"))
                continue
        if s.cap is not None and counts.get(key, 0) >= s.cap:
            skip("deferred_cap", None, cap=s.cap)
            continue
        counts[key] = counts.get(key, 0) + 1
        e["codes"] = source_codes(key, lines, ids)
        pl.by_source.setdefault(key, []).append(e)
    speeds = load_speeds(cfg)
    per = [SOURCES[k].list_s + len(v) * s_per_company(cfg, k, speeds) for k, v in pl.by_source.items() if v]
    pl.est_seconds = round(min(time_s, max(per, default=0.0)), 1)
    pl.est_mb = round(sum(len(v) * SOURCES[k].mb_per_company for k, v in pl.by_source.items()), 1)
    return pl


# ---------------------------------------------------------------------------------------------------------------
# Console lines

def _dur(seconds: float, lang: str) -> str:
    if seconds >= 60 and seconds % 60 == 0:
        m = int(seconds // 60)
        return f"{m} 分钟" if lang == "zh" else f"{m} min"
    return f"{seconds:.0f} 秒" if lang == "zh" else f"{seconds:.0f} s"


def _per_source(counts: Mapping[str, int], lang: str, totals: Mapping[str, int] | None = None) -> str:
    return " · ".join(f"{label(k, lang)} {n}" + (f"/{totals[k]}" if totals else "") for k, n in counts.items())


def start_line(pl: Plan, lang: str, time_s: float) -> str:
    total = len(pl.entries)
    n = pl.n_planned
    rest = total - n
    res = sum(1 for e in pl.entries.values() if e.get("rescued"))     # L1 misses read anyway (screen.l1_rescued)
    if lang == "zh":
        line = (f"补抓年报：{total} 家公司本地没有年报原文（通过第一轮的 {total - res} 家，第一轮没通过、因简介太薄或"
                f"偏题要照读年报的 {res} 家）" if res else f"补抓年报：通过第一轮的 {total} 家公司本地没有年报原文")
        if n:
            line += (f"；其中 {n} 家现在从官方网站免费下载（最多 {_dur(time_s, lang)}，约 {pl.est_mb:.0f} MB）："
                     + _per_source(pl.planned, lang))
        else:
            line += "；这次没有可以下载的"
        if rest:
            line += f"；另外 {rest} 家这次不抓（见报告“年报原文与缺口”）"
        return line
    line = (f"Fetching annual reports: {total} companies have no annual-report text stored ({total - res} passed the "
            f"first round, {res} missed it but are read anyway for a thin or one-sided profile)" if res else
            f"Fetching annual reports: {total} companies that passed the first round have no annual-report text stored")
    if n:
        line += (f"; downloading {n} now from official sites (free, at most {_dur(time_s, lang)}, about "
                 f"{pl.est_mb:.0f} MB): " + _per_source(pl.planned, lang))
    else:
        line += "; none can be downloaded this time"
    if rest:
        line += f"; {rest} are not fetched this time (see 'Annual-report evidence and gaps' in the report)"
    return line


def heartbeat_line(elapsed: float, time_s: float, done: Mapping[str, int], planned: Mapping[str, int],
                   lang: str) -> str:
    d, t = sum(done.values()), sum(planned.values())
    parts = _per_source({k: done.get(k, 0) for k in planned}, lang, planned)
    if lang == "zh":
        return f"补抓中（{elapsed:.0f} 秒/最多 {time_s:.0f} 秒）：已处理 {d}/{t} · {parts}"
    return f"Fetching ({elapsed:.0f} s of at most {time_s:.0f} s): {d}/{t} done · {parts}"


def slow_line(key: str, lang: str) -> str:
    return (f"{label(key, lang)} 网络很慢，这次能抓到的会少一些" if lang == "zh"
            else f"{label(key, lang)} is slow from here; fewer will be fetched this time")


def interrupt_line(n_fetched: int, lang: str) -> str:
    if lang == "zh":
        return f"已停止补抓；用已抓到的 {n_fetched} 家更新结果（几秒）。再按一次 Ctrl-C 直接退出，第一版报告保留"
    return (f"Fetch stopped; updating the results with the {n_fetched} reports already fetched (a few seconds). Press "
            "Ctrl-C again to quit; the first report is kept")


def end_line(n_new: int, cost: float, n_profile: int, lang: str) -> str:
    if lang == "zh":
        return (f"已更新报告：新增 {n_new} 家年报原文，花费 ${cost:.3f}；{n_profile} 家仍只读简介（见报告“年报原文与缺口”）")
    return (f"Report updated: {n_new} more {'company' if n_new == 1 else 'companies'} now read from annual reports, "
            f"cost ${cost:.3f}; {n_profile} still read the profile only (see 'Annual-report evidence and gaps')")


DIFF_TITLE_ZH = "更新（新抓年报，L1 沿用 $0）"


def summary_sentence(lang: str, fr: Mapping[str, Any]) -> str:
    """'62 家读了官方年报（其中 27 家本次新抓）；20 家只读了简介：16 家市场暂无来源，4 家时间到'. fr: annual, fetched,
    since (stored by an earlier fetch, read now), pending (stored, but the results were not updated: counted apart,
    with next), profile, by_reason ({code: n} over the companies still on a profile). annual + pending + profile is
    every layer-2 input of the run."""
    annual, fetched, profile = int(fr.get("annual") or 0), int(fr.get("fetched") or 0), int(fr.get("profile") or 0)
    since, pending, nxt = int(fr.get("since") or 0), int(fr.get("pending") or 0), fr.get("next") or _LATER
    parts = sorted(((n, c) for c, n in (fr.get("by_reason") or {}).items() if n and c not in NOT_GAPS),
                   key=lambda x: (-x[0], x[1]))
    if lang == "zh":
        new = [f"{fetched} 家本次新抓"] * bool(fetched) + [f"{since} 家之前已抓到"] * bool(since)
        s = f"{annual} 家读了官方年报" + (f"（其中 {'，'.join(new)}）" if new else "")
        if pending:
            s += f"；另有 {pending} 家年报已存到本地，但结果还没更新（下一步：{nxt}）"
        if profile:
            s += f"；{profile} 家只读了简介"
            if parts:
                s += "：" + "，".join(f"{n} 家{short_reason(c, lang)}" for n, c in parts)
        return s
    new = [f"{fetched} fetched now"] * bool(fetched) + [f"{since} stored earlier"] * bool(since)
    s = f"{annual} read official annual reports" + (f" ({', '.join(new)})" if new else "")
    if pending:
        s += (f"; {pending} more have an annual report stored but the results are not updated yet (next: {nxt})")
    if profile:
        s += f"; {profile} read the profile only"
        if parts:
            s += ": " + ", ".join(f"{n} {short_reason(c, lang)}" for n, c in parts)
    return s


def readiness_lines(rd: Mapping[str, Any], lang: str) -> list[str]:
    out = []
    if not rd.get("sec_email"):
        out.append("未设置 SEC 联系邮箱：美国公司只能读简介" if lang == "zh"
                   else "No SEC contact email: US companies will read the profile only")
    if not rd.get("pdf_reader"):
        out.append(f"没装读 PDF 的组件：A股/印度/台湾年报暂时读不了（{_PIP}）" if lang == "zh"
                   else f"No PDF reader installed: China/India/Taiwan reports can't be read yet ({_PIP})")
    return out


DRY_RUN_SUFFIX = {"zh": "（另加最多 {m}，免费补抓年报：只抓第二步要读、本地没有年报原文的公司）",
                  "en": " (+ up to {m}, free, fetching annual reports for the companies step 2 reads with none stored)"}


def dry_run_suffix(lang: str, time_s: float = FETCH_TIME_DEFAULT_S) -> str:
    return DRY_RUN_SUFFIX[lang].format(m=_dur(time_s, lang))


# ---------------------------------------------------------------------------------------------------------------
# Launch (parent orchestrator) and the child

def _pkg_root() -> str:
    return str(Path(__file__).resolve().parents[1])


def child_command(cfg: Any, key: str, plan_path: Path) -> list[str]:
    return [sys.executable, "-m", "jevscreen.ondemand", "child", "--source", key, "--plan", str(plan_path),
            "--home", str(cfg.home)]


def _count_lines(path: Path) -> tuple[int, int]:
    """(events, ok events) in an events file (0, 0 when missing)."""
    n = ok = 0
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                n += 1
                if '"status": "ok"' in line:
                    ok += 1
    except OSError:
        pass
    return n, ok


def read_events(path: Path) -> list[dict[str, Any]]:
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                with contextlib.suppress(ValueError):
                    ev = json.loads(line)
                    if isinstance(ev, dict):
                        out.append(ev)
    except OSError:
        pass
    return out


def read_summary(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


@dataclass
class Child:
    key: str
    proc: Any
    terminated: str | None = None      # 'deadline' | 'interrupt'
    killed: bool = False


def launch(cfg: Any, pl: Plan, *, time_s: float, fetch_dir: Path, retry_failed: bool = False, lang: str = "en",
           out: Callable[[str], None] = print, popen: Callable[..., Any] | None = None,
           clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
           wall: Callable[[], float] = time.time, grace_s: float = GRACE_S, heartbeat_s: float = HEARTBEAT_S,
           poll_s: float = POLL_S, mode: str = "auto",
           on_progress: Callable[[int, int], None] | None = None) -> dict[str, Any]:
    """Run one child per planned source until they finish or the deadline passes, then collect the outcomes in one
    read-only session. on_progress(done, total) (quickstart's progress line): companies settled so far over the
    planned companies, called at the start, whenever the count changes and at the end; its errors are ignored. Returns {'summary' (layers.fetch), 'outcomes' {ck: {source, code, note, ...}}, 'fetched' [ck],
    'docs_by_ck', 'interrupted'}. The first KeyboardInterrupt stops the children (SIGTERM, GRACE_S) and continues to
    the outcomes; a second one propagates. The parent opens no store session while a child is alive."""
    from . import store
    if popen is None and _real_adapters_refused():
        raise RealAdaptersRefused(f"{TESTING_ENV}=1: launch() would start the real adapters (pass popen or set "
                                  f"{MODULES_ENV})")
    fetch_dir = Path(fetch_dir)
    fetch_dir.mkdir(parents=True, exist_ok=True)
    planned = pl.planned
    t_start = clock()
    deadline_epoch = wall() + time_s
    plan_path = fetch_dir / "plan.json"
    plan_path.write_text(json.dumps({"run_id": pl.run_id, "deadline_epoch": deadline_epoch, "grace_s": grace_s,
                                     "sources": {k: {"codes": sorted({c for e in v for c in e.get("codes") or []}),
                                                     "companies": [e["company_key"] for e in v],
                                                     "refresh": bool(retry_failed)}
                                                 for k, v in pl.by_source.items() if v}},
                                    indent=2, sort_keys=True), encoding="utf-8")
    popen = popen or subprocess.Popen
    env = dict(os.environ)
    env["PYTHONPATH"] = _pkg_root() + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["JEVSCREEN_STORE_WAIT_S"] = str(int(time_s + grace_s))
    env["JEVSCREEN_HOME"] = str(cfg.home)
    children: dict[str, Child] = {}
    slow_said: set[str] = set()
    interrupted = False

    def alive() -> list[Child]:
        return [c for c in children.values() if c.proc.poll() is None]

    def done_counts() -> dict[str, int]:
        return {k: _count_lines(fetch_dir / f"{k}.events.jsonl")[0] for k in planned}

    def terminate(why: str) -> None:
        for c in alive():
            if c.terminated is None:
                c.terminated = why
                with contextlib.suppress(OSError, ProcessLookupError):
                    c.proc.terminate()

    def kill_rest() -> None:
        for c in alive():
            c.killed = True
            with contextlib.suppress(OSError, ProcessLookupError):
                c.proc.kill()
        for c in children.values():
            with contextlib.suppress(Exception):
                c.proc.wait(timeout=5)

    def wait_until(t_end: float) -> None:
        while alive() and clock() < t_end:
            sleep(min(poll_s, max(0.01, t_end - clock())))

    next_hb = t_start + heartbeat_s
    kill_at: float | None = None
    speeds = load_speeds(cfg)
    total = sum(planned.values())
    told = [-1]

    def tell(n: int) -> None:
        if on_progress is not None and n != told[0]:
            told[0] = n
            with contextlib.suppress(Exception):
                on_progress(min(n, total), total)
    tell(0)
    try:
        # started inside the guard: an interrupt (Ctrl-C / SIGTERM as KeyboardInterrupt) while children are being
        # started stops the ones already running, like one during the fetch
        try:
            for key in planned:
                with contextlib.suppress(OSError):
                    (fetch_dir / f"{key}.events.jsonl").unlink()
                    (fetch_dir / f"{key}.summary.json").unlink()
                log = open(fetch_dir / f"{key}.log", "ab")
                try:
                    proc = popen(child_command(cfg, key, plan_path), stdout=log, stderr=subprocess.STDOUT, env=env,
                                 stdin=subprocess.DEVNULL)
                finally:
                    log.close()
                children[key] = Child(key, proc)
        except Exception:
            kill_rest()           # a child that cannot be started (OSError): stop the others, then report it
            raise
        while alive():
            now = clock()
            if now - t_start >= time_s and kill_at is None:
                terminate("deadline")
                kill_at = now + grace_s
            if kill_at is not None and now >= kill_at:
                kill_rest()
                break
            if now >= next_hb:
                out(heartbeat_line(now - t_start, time_s, done_counts(), planned, lang))
                next_hb += heartbeat_s
            if on_progress is not None:
                tell(sum(done_counts().values()))
            for key in planned:
                if key in slow_said:
                    continue
                n, _ = _count_lines(fetch_dir / f"{key}.events.jsonl")
                est = SOURCES[key].list_s + SLOW_AFTER * s_per_company(cfg, key, speeds)
                if n < min(SLOW_AFTER, planned[key]) and now - t_start > SLOW_FACTOR * est \
                        and children[key].proc.poll() is None:
                    slow_said.add(key)
                    out(slow_line(key, lang))
                elif n >= SLOW_AFTER:
                    slow_said.add(key)
            sleep(poll_s)
    except KeyboardInterrupt:
        interrupted = True
        n_ok = sum(_count_lines(fetch_dir / f"{k}.events.jsonl")[1] for k in planned)
        out(interrupt_line(n_ok, lang))
        terminate("interrupt")
        wait_until(clock() + grace_s)      # a second KeyboardInterrupt propagates: the phase-1 report stays
        kill_rest()
    kill_rest()
    tell(sum(done_counts().values()))
    seconds = round(clock() - t_start, 1)
    events = {k: read_events(fetch_dir / f"{k}.events.jsonl") for k in planned}
    summaries = {k: read_summary(fetch_dir / f"{k}.summary.json") for k in planned}
    child_info = {k: {"terminated": c.terminated, "killed": c.killed,
                      "returncode": getattr(c.proc, "returncode", None)} for k, c in children.items()}
    docs: dict[str, Any] | None = None
    idmap: dict[str, set[str]] | None = None
    store_ok = True
    try:
        with store.session(cfg, read_only=True, wait_s=OUTCOME_WAIT_S) as con:
            docs, idmap = _outcome_reads(con, pl)
    except store.StoreLocked:
        store_ok = False
    oc = outcomes(pl, events, summaries, child_info, docs, idmap)
    record_skips(cfg, oc)
    for k in planned:      # measured s/company: companies that were actually fetched or tried, not skips
        s = summaries.get(k) or {}
        record_speed(cfg, k, float(s.get("seconds") or 0.0),
                     sum(1 for ev in events.get(k) or [] if ev.get("status") not in SKIP_STATUSES))
    fetched = [ck for ck, o in oc.items() if o["code"] == "fetched"]
    summary = fetch_summary(pl, oc, summaries, child_info, events, seconds=seconds, time_s=time_s,
                            interrupted=interrupted, mode=mode)
    if not store_ok:
        summary["update"] = {"skipped": "store_busy"}
    return {"summary": summary, "outcomes": oc, "fetched": fetched,
            "docs_by_ck": {k: docs[k] for k in fetched} if docs else {},
            "interrupted": interrupted, "store_ok": store_ok}


def _outcome_reads(con, pl: Plan) -> tuple[dict[str, Any], dict[str, set[str]]]:
    """(readable documents by company_key, {security_id: {id_type}}) after the fetch."""
    from . import screen
    docs = screen.load_documents(con, min_mcap_usd=float(pl.params.get("min_mcap_usd") or 0.0))
    docs = {ck: d for ck, d in docs.items() if ck in pl.entries and _readable(d)}
    sids = sorted({ln["security_id"] for e in pl.entries.values() for ln in e.get("lines") or []})
    idmap: dict[str, set[str]] = {}
    if sids:
        for sid, t in con.execute("SELECT security_id, id_type FROM identifiers "
                                  "WHERE list_contains(?::VARCHAR[], security_id)", [sids]).fetchall():
            idmap.setdefault(sid, set()).add(t)
    return docs, idmap


def outcomes(pl: Plan, events: Mapping[str, list[dict]], summaries: Mapping[str, dict | None],
             child_info: Mapping[str, Mapping[str, Any]], docs: Mapping[str, Any] | None,
             idmap: Mapping[str, set[str]] | None) -> dict[str, dict[str, Any]]:
    """{company_key: {source, code, note, ...}} for every profile-only L2 input: the plan's skip reason, else (first
    match) fetched (a readable document now) / the company's last event (STATUS_TO_REASON; an 'ok' event without a
    readable document is store_busy when a store lock got in the way, deferred_time when the child was stopped
    before its flush, else extract_failed; an adapter skip is known_failure with today's date and SKIP_CACHE_DAYS) /
    not_mapped (no event, no identifier of the source's id_type, child finished normally) / deferred_time (child
    stopped by the deadline, an interrupt or a kill) / source_busy / blocked / store_busy / unresolved."""
    out: dict[str, dict[str, Any]] = {}
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    for ck, _src, reason, note in pl.skipped:
        e = pl.entries.get(ck) or {}
        out[ck] = {"source": e.get("source"), "code": reason, "note": note,
                   **{k: e[k] for k in ("date", "days", "cap", "when") if k in e}}
    for key, ents in pl.by_source.items():
        by_sid: dict[str, dict] = {}
        for ev in events.get(key) or []:
            for sid in ev.get("security_ids") or []:
                by_sid[sid] = ev
        s = summaries.get(key) or {}
        info = child_info.get(key) or {}
        st = s.get("status")
        stopped = info.get("terminated") or info.get("killed") or st == "interrupted" or s.get("stopped_reason") in (
            "interrupted", "deadline")
        for e in ents:
            ck = e["company_key"]
            sids = [ln["security_id"] for ln in e.get("lines") or []] or [e["security_id"]]
            ev = next((by_sid[sid] for sid in sids if sid in by_sid), None)
            if docs is not None and ck in docs:
                out[ck] = {"source": key, "code": "fetched", "note": None}
                continue
            if ev is not None:
                status = str(ev.get("status") or "")
                code = STATUS_TO_REASON.get(status, "unresolved")
                o = {"source": key, "code": code, "note": ev.get("note"), "status": status}
                if status == "ok":
                    # adapters report 'ok' when the row joins their batch, before it is written: no readable document
                    # means the store stayed locked (parent or child) or the child was stopped before its flush
                    o["note"] = ev.get("note") or "no_readable_text"
                    if docs is None or st == "store_locked":
                        o["code"] = "store_busy"
                    elif stopped or not s:
                        o["code"] = "deferred_time"
                elif status in SKIP_STATUSES:
                    # the newest report was tried before and is still unreadable; remembered (record_skips) for
                    # SKIP_CACHE_DAYS
                    o.update(note=ev.get("note") or status, date=today, days=SKIP_CACHE_DAYS)
                out[ck] = o
                continue
            mapped = idmap is None or any(SOURCES[key].id_type in (idmap.get(sid) or set()) for sid in sids)
            if st == "source_busy":
                code = "source_busy"
            elif st == "blocked":
                code = "blocked"
            elif st == "store_locked":
                code = "store_busy"
            elif stopped:
                code = "deferred_time"
            elif not mapped and st in ("ok", "stopped_errors"):
                code = "not_mapped"
            else:
                code = "unresolved"
            out[ck] = {"source": key, "code": code, "note": None}
    return out


def questions_for(pl: Plan, by_reason: Mapping[str, int]) -> list[dict[str, Any]]:
    """ask_human questions, only when a company of this run is affected (and never again after a recorded 'no'). An
    L1 miss read anyway (screen.l1_rescued) does not count: the human is not asked for a company step 1 rejected."""
    rescued: dict[str, int] = {}
    for e in pl.entries.values():
        if e.get("rescued") and e.get("reason"):
            rescued[e["reason"]] = rescued.get(e["reason"], 0) + 1
    by_reason = {k: n - rescued.get(k, 0) for k, n in by_reason.items()}
    out = []
    # then_command: after a yes (the key / the consent recorded) it fetches this run's missing reports and updates
    # the report (free fetch, update at most $0.05); after a no nothing more is needed
    then = shlex.join(["jevscreen", "fetch-docs", pl.run_id]) if pl.run_id else "jevscreen fetch-docs latest"
    if by_reason.get("no_key_sec") and pl.readiness.get("sec_email_ask") != "no":
        q = QUESTIONS["sec_email"]
        out.append({"id": "sec_email", **q, "then_command": then, "ask_human": True})
    n_tw = by_reason.get("mops_off", 0)
    if n_tw and pl.readiness.get("mops_consent") is None:
        q = QUESTIONS["mops_annual"]
        out.append({"id": "mops_annual", "human_question_zh": q["human_question_zh"].format(n=n_tw),
                    "human_question_en": q["human_question_en"].format(n=n_tw),
                    "record_answer_commands": q["record_answer_commands"], "then_command": then, "ask_human": True})
    return out


def fetch_summary(pl: Plan, oc: Mapping[str, Mapping[str, Any]], summaries: Mapping[str, dict | None],
                  child_info: Mapping[str, Mapping[str, Any]], events: Mapping[str, list],
                  *, seconds: float, time_s: float, interrupted: bool = False, mode: str = "auto") -> dict[str, Any]:
    """layers.fetch (docs/AGENT_API.md)."""
    by_reason: dict[str, int] = {}
    for o in oc.values():
        by_reason[o["code"]] = by_reason.get(o["code"], 0) + 1
    fetched: dict[str, int] = {}
    for o in oc.values():
        if o["code"] == "fetched":
            fetched[o["source"]] = fetched.get(o["source"], 0) + 1
    sources = {}
    for k in pl.planned:
        s = summaries.get(k) or {}
        info = child_info.get(k) or {}
        n_ev = len(events.get(k) or [])
        sec = float(s.get("seconds") or 0.0)
        status = s.get("status") or ("killed" if info.get("killed") else "unknown")
        sources[k] = {"status": status, "stopped_reason": s.get("stopped_reason") or info.get("terminated"),
                      "requests": s.get("requests"), "seconds": round(sec, 1),
                      "s_per_company": round(sec / n_ev, 2) if n_ev else None,
                      "mb": round(float(s.get("bytes_downloaded") or 0) / 1e6, 1),
                      "agent_action": "none"}          # a block / busy source is already handled: nothing to do
    abandoned = [k for k, i in child_info.items() if i.get("killed")]
    deferred = sum(by_reason.get(c, 0) for c in DEFERRED)
    bad = any(v["status"] not in ("ok",) for v in sources.values())
    status = "interrupted" if interrupted else ("partial" if (bad or deferred or abandoned) else "ok")
    return {"mode": mode, "status": status, "skip_reason": None, "base_run": pl.run_id, "time_budget_s": time_s,
            "seconds": seconds, "planned": pl.planned, "fetched": fetched, "by_reason": dict(sorted(by_reason.items())),
            "sources": sources, "abandoned": abandoned,
            "readiness": {k: pl.readiness.get(k) for k in ("pdf_reader", "sec_email", "opendart", "mops_consent")},
            "questions": questions_for(pl, by_reason), **summary_texts(pl, oc), "next_command": None,
            "update": None}


def summary_texts(pl: Plan, oc: Mapping[str, Mapping[str, Any]], pending_next: str | None = None
                  ) -> dict[str, str]:
    """{'summary_zh', 'summary_en'}. pending_next=None: the results read every fetched / stored_since company (the
    update pass ran); else they are counted as stored-but-not-read-yet, with that next command."""
    by_reason: dict[str, int] = {}
    for o in oc.values():
        by_reason[o["code"]] = by_reason.get(o["code"], 0) + 1
    n_fetched, n_since = by_reason.get("fetched", 0), by_reason.get("stored_since", 0)
    gaps = {c: n for c, n in by_reason.items() if c not in NOT_GAPS}
    fr: dict[str, Any] = {"annual": pl.stored_n + n_fetched + n_since, "fetched": n_fetched, "since": n_since,
                          "profile": sum(gaps.values()), "by_reason": gaps}
    if pending_next is not None:
        fr.update(annual=pl.stored_n, fetched=0, since=0, pending=n_fetched + n_since, next=pending_next)
    return {"summary_zh": summary_sentence("zh", fr), "summary_en": summary_sentence("en", fr)}


def skipped_summary(pl: Plan | None, reason: str, *, mode: str = "auto", time_s: float = 0.0,
                    base_run: str | None = None) -> dict[str, Any]:
    """layers.fetch when the fetch phase did not run (reason: store_busy, nothing_planned, ...)."""
    oc = {ck: {"source": e.get("source"), "code": e.get("reason") or reason, "note": e.get("note")}
          for ck, e in (pl.entries.items() if pl else [])}
    if pl is not None and pl.store_busy:
        oc = {}
    s = fetch_summary(pl or Plan(run_id=base_run or ""), oc, {}, {}, {}, seconds=0.0, time_s=time_s, mode=mode)
    s.update(status="skipped", skip_reason=reason)
    return s


def outcome_rows(pl: Plan, oc: Mapping[str, Mapping[str, Any]], *, pending: bool = False) -> list[dict[str, Any]]:
    """gaps['l2_doc_unavailable'] rows: the companies still on a profile, with source and reason. pending=True (the
    update pass did not run): the fetched / stored_since companies still read the profile too, as 'update_pending'."""
    rows = []
    for ck in pl.order:
        o = oc.get(ck)
        e = pl.entries[ck]
        if not o or (o["code"] in NOT_GAPS and not pending):
            continue
        rows.append({"security_id": e["security_id"], "name": e.get("name"), "country": e.get("country"),
                     "exchange": e.get("exchange"), "market_cap_usd": e.get("market_cap_usd"),
                     "source": o.get("source"), "reason": "update_pending" if o["code"] in NOT_GAPS else o["code"],
                     "note": o.get("note"),
                     **{k: o[k] for k in ("date", "days", "cap", "when") if k in o}})
    return rows


def fetch_info(result: Mapping[str, Any]) -> dict[str, Any]:
    """The screen(fetch_info=...) argument of the update pass."""
    return {"summary": result["summary"], "fetched": pending_update(result),
            "outcomes": {ck: dict(o) for ck, o in result["outcomes"].items()}}


def pending_update(result: Mapping[str, Any]) -> list[str]:
    """The companies the update pass should read from an annual report: fetched now, or stored since the run read
    their profile (an earlier fetch whose update was skipped)."""
    return list(result["fetched"]) + [ck for ck, o in result["outcomes"].items()
                                      if o.get("code") == "stored_since" and ck not in result["fetched"]]


# ---------------------------------------------------------------------------------------------------------------
# Gap lines (report.md, one language)

def gap_lines(items: Sequence[Mapping[str, Any]], lang: str) -> list[str]:
    """Grouped lines, one per reason (and per source for paused / blocked sources): 'Indonesia 29 · Vietnam 19:
    no official annual-report source for these markets yet', '52 US companies read the profile only: no SEC contact
    email set'."""
    by_reason: dict[str, list[Mapping[str, Any]]] = {}
    for it in items:
        by_reason.setdefault(str(it.get("reason") or "unresolved"), []).append(it)
    order = sorted(by_reason, key=lambda r: (-len(by_reason[r]), r))
    out = []
    for r in order:
        its = by_reason[r]
        markets: dict[str, int] = {}
        for it in its:
            m = market_name(it.get("country"), lang)
            markets[m] = markets.get(m, 0) + 1
        mk = " · ".join(f"{m} {n}" for m, n in sorted(markets.items(), key=lambda x: (-x[1], x[0])))
        if r == "no_adapter":
            out.append(f"{mk}：这些市场还没有官方年报来源" if lang == "zh"
                       else f"{mk}: no official annual-report source for these markets yet")
            continue
        if r in ("source_paused", "blocked", "deferred_cap", "unresolved"):
            by_src: dict[str | None, list] = {}
            for it in its:
                by_src.setdefault(it.get("source"), []).append(it)
            for src, grp in by_src.items():
                first = grp[0]
                text = reason_text(r, lang, source=label(src, lang), cap=first.get("cap"), when=first.get("when"),
                                   src=src or "-")
                out.append(f"{len(grp)} 家（{label(src, lang)}）：{text}" if lang == "zh"
                           else f"{len(grp)} ({label(src, lang)}): {text}")
            continue
        if r in ("extract_failed", "known_failure"):
            text = reason_text(r, lang, note=its[0].get("note"), date=its[0].get("date"), days=its[0].get("days"))
            if len(its) > 1:
                text = short_reason(r, lang) + ("（逐家原因见 results.json）" if lang == "zh"
                                                else " (per-company reasons in results.json)")
        else:
            text = reason_text(r, lang)
        nxt = REASONS.get(r, {}).get("next_command")
        tail = (f"（下一步：{nxt}）" if lang == "zh" else f" (next: {nxt})") if nxt and not REASONS[r]["ask_human"] else ""
        if len(markets) == 1:
            m = next(iter(markets))
            who = f"{len(its)} 家{m}公司" if lang == "zh" else f"{len(its)} {m} compan{'y' if len(its) == 1 else 'ies'}"
        else:
            who = f"{len(its)} 家公司（{mk}）" if lang == "zh" else f"{len(its)} companies ({mk})"
        out.append((f"{who}只读了简介：{text}" if lang == "zh" else f"{who} read the profile only: {text}") + tail)
    return out


# ---------------------------------------------------------------------------------------------------------------
# Doctor checks (agent-ops)

SEC_QUESTION_EN = QUESTIONS["sec_email"]["human_question_en"]


def doctor_checks(cfg: Any) -> list[dict[str, Any]]:
    """on_demand_docs (PDF reader + SEC email) and mops_annual (skip; its question is asked only when a screen's
    questions[] carries it)."""
    rd = readiness(cfg)
    out: list[dict[str, Any]] = []
    base = {"ask_human": False}
    if not rd["pdf_reader"]:
        out.append({"id": "on_demand_docs", "status": "warn", "detail": "no PDF reader: China / India / Taiwan annual "
                    "reports cannot be read during a screen (companies there read the profile only)",
                    "fix_command": _PIP, **base})
    elif not rd["sec_email"] and rd.get("sec_email_ask") == "no":
        out.append({"id": "on_demand_docs", "status": "skip", "detail": "no SEC contact email (the human said no: "
                    "US companies without stored annual-report text read the profile only)", "fix_command": None,
                    **base})
    elif not rd["sec_email"]:
        out.append({"id": "on_demand_docs", "status": "warn", "detail": "no SEC contact email: US companies without "
                    "stored annual-report text read the profile only during a screen; the name and email go only to "
                    "sec.gov, so ask the human", "fix_command": None, "ask_human": True,
                    "human_question": SEC_QUESTION_EN,
                    "record_answer_commands": QUESTIONS["sec_email"]["record_answer_commands"]})
    else:
        out.append({"id": "on_demand_docs", "status": "ok", "detail": f"screens fetch missing annual reports on "
                    f"demand (PDF reader: {', '.join(rd['backends'])}; SEC email set)", "fix_command": None, **base})
    state = rd["mops_consent"]
    q = QUESTIONS["mops_annual"]
    out.append({"id": "mops_annual", "status": "ok" if state == "yes" else "skip",
                "detail": {"yes": "Taiwan annual-report downloads (MOPS) are on",
                           "no": "Taiwan annual-report downloads (MOPS) are off (the human said no)"}.get(
                    state, "Taiwan annual-report downloads (MOPS) are off by default; ask only when a screen's "
                           "questions[] includes mops_annual"),
                "fix_command": None, "ask_human": False,
                **({"human_question": q["human_question_en"].format(n="Some"),
                    "record_answer_commands": q["record_answer_commands"]} if state is None else {})})
    return out


# ---------------------------------------------------------------------------------------------------------------
# Child process: python -m jevscreen.ondemand child --source KEY --plan PLAN.json --home HOME

def child_main(argv: Sequence[str]) -> int:
    ap = argparse.ArgumentParser(prog="jevscreen.ondemand child")
    ap.add_argument("--source", required=True, choices=sorted(SOURCES))
    ap.add_argument("--plan", required=True, type=Path)
    ap.add_argument("--home", required=True, type=Path)
    args = ap.parse_args(list(argv))
    from . import guard
    from .config import Config, redact
    cfg = Config(home=args.home)
    src = SOURCES[args.source]
    fetch_dir = args.plan.parent
    events_path = fetch_dir / f"{src.key}.events.jsonl"
    summary_path = fetch_dir / f"{src.key}.summary.json"
    spec_all = json.loads(args.plan.read_text(encoding="utf-8"))
    spec = (spec_all.get("sources") or {}).get(src.key) or {}
    deadline = float(spec_all.get("deadline_epoch") or (time.time() + FETCH_TIME_DEFAULT_S))
    t0 = time.monotonic()
    state = {"signal": None, "events": 0}

    def on_signal(signum, frame):  # noqa: ARG001
        for s in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(ValueError, OSError):
                signal.signal(s, signal.SIG_IGN)
        state["signal"] = "deadline" if time.time() >= deadline - 1.0 else "interrupted"
        raise KeyboardInterrupt(state["signal"])

    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, on_signal)
    timer = threading.Timer(max(0.0, deadline - time.time()), lambda: os.kill(os.getpid(), signal.SIGTERM))
    timer.daemon = True
    timer.start()
    secrets = [cfg.sec_user_agent()] if src.key == "sec" else []
    summary: dict[str, Any] = {"source": src.key, "status": None, "stopped_reason": None, "requests": 0}

    def write_summary() -> None:
        summary.update(seconds=round(time.monotonic() - t0, 2), events=state["events"])
        tmp = summary_path.with_suffix(".tmp")
        tmp.write_text(redact(json.dumps(summary, default=str, ensure_ascii=False, sort_keys=True), secrets),
                       encoding="utf-8")
        os.replace(tmp, summary_path)

    code = 1
    try:
        with guard.budget_lock(cfg, src.budget):
            mod = importlib.import_module(adapter_module(src.key))
            with open(events_path, "a", encoding="utf-8") as fh:
                def writer(sids: list[str], status: str, note: str | None) -> None:
                    fh.write(json.dumps({"t": round(time.time(), 3), "security_ids": list(sids), "status": status,
                                         "note": redact(note, secrets) if note else None}, ensure_ascii=False)
                             + "\n")
                    fh.flush()
                    state["events"] += 1
                res = mod.sync(cfg, codes=list(spec.get("codes") or []), refresh=bool(spec.get("refresh")),
                               only_universe=True, progress_every=10 ** 9, on_company=writer,
                               **dict(src.sync_kwargs))
            res = res if isinstance(res, dict) else {}
            summary.update({k: v for k, v in res.items() if k not in ("ambiguous",)})
            status = str(res.get("status") or "ok")
            summary["status"] = status
            if status == "interrupted" or res.get("stopped_reason") == "interrupted":
                summary["stopped_reason"] = state["signal"] or "interrupted"
            code = CHILD_EXIT.get(status, 1)
    except guard.Busy as e:
        summary.update(status="source_busy", note=str(e)[:200])
        code = CHILD_EXIT["source_busy"]
    except KeyboardInterrupt:
        summary.update(status="interrupted", stopped_reason=state["signal"] or "interrupted")
        code = CHILD_EXIT["interrupted"]
    except Exception as e:  # noqa: BLE001 - recorded, the parent maps it to the companies
        from . import store
        name = type(e).__name__
        summary.update(status="store_locked" if isinstance(e, store.StoreLocked) else "error",
                       note=redact(f"{name}: {e}", secrets)[:300])
        code = 1
    finally:
        timer.cancel()
        for s in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(ValueError, OSError):
                signal.signal(s, signal.SIG_IGN)
        with contextlib.suppress(OSError):
            write_summary()
    return code


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["child"]:
        return child_main(argv[1:])
    print("usage: python -m jevscreen.ondemand child --source KEY --plan PLAN.json --home HOME", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
